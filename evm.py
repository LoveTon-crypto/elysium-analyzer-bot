"""Minimal EVM toolkit for Elysium: JSON-RPC client, ABI encoding for the few calls we need, tx sending.

ABI is encoded by hand (static uint/address args + one address[] path, string/uint returns) so the only
third-party crypto code is eth-account, used for signing.
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import re
import time
from decimal import Decimal

import httpx
from eth_utils import to_checksum_address

log = logging.getLogger("evm")

WEI = 10 ** 18
ADDR_RE = re.compile(r"0x[0-9a-fA-F]{40}")
UINT_MAX = 2 ** 256 - 1


class RpcError(RuntimeError):
    pass


import os as _os
# eth_getLogs pacing per node, shared by every Rpc client in the process (the nodes limit log searches per IP)
LOGS_PER_MIN = float(_os.getenv("LOGS_PER_MIN_PER_NODE", "40"))
_LOG_NEXT: dict[str, float] = {}       # url → earliest monotonic time for its next log search
_LOG_COOL: dict[str, float] = {}       # url → rate-limited until
_HEIGHTS: dict[str, int] = {}          # url → last seen head (from the health check)
_LOG_LOCK = None


def _rate_limited(msg: str) -> bool:
    m = msg.lower()
    return "too many" in m or "rate limit" in m or "rate-limit" in m or "429" in m


class TxFailed(RuntimeError):
    def __init__(self, msg: str, tx: str | None = None):
        super().__init__(msg)
        self.tx = tx


# ---------------------------------------------------------------- ABI
def _word(v: int) -> str:
    if v < 0 or v > UINT_MAX:
        raise ValueError("uint256 out of range")
    return f"{v:064x}"


def _addr(a: str) -> str:
    if not ADDR_RE.fullmatch(a):
        raise ValueError(f"bad address {a!r}")
    return a[2:].lower().rjust(64, "0")


def encode(selector: str, *args) -> str:
    """args: int → uint256, '0x…' str → address, list[str] → address[] (only as the dynamic tail)."""
    head, tail = [], []
    n = len(args)
    for a in args:
        if isinstance(a, list):
            head.append(None)
            tail.append(a)
        elif isinstance(a, str):
            head.append(_addr(a))
        else:
            head.append(_word(int(a)))
    out, offset, dyn = [], 32 * n, []
    for h in head:
        if h is None:
            arr = tail.pop(0)
            out.append(_word(offset))
            body = _word(len(arr)) + "".join(_addr(x) for x in arr)
            dyn.append(body)
            offset += len(body) // 2
        else:
            out.append(h)
    return selector + "".join(out) + "".join(dyn)


def words(data: str) -> list[int]:
    d = data[2:] if data.startswith("0x") else data
    return [int(d[i:i + 64], 16) for i in range(0, len(d) - len(d) % 64, 64)]


def dec_uint(data: str, i: int = 0) -> int:
    return words(data)[i]


def dec_addr(data: str, i: int = 0) -> str:
    return "0x" + f"{words(data)[i]:064x}"[24:]


def dec_string(data: str) -> str:
    d = data[2:] if data.startswith("0x") else data
    if not d:
        return ""
    if len(d) == 64:  # old tokens return bytes32
        return bytes.fromhex(d).rstrip(b"\0").decode("utf-8", "replace")
    off = int(d[:64], 16) * 2
    ln = int(d[off:off + 64], 16)
    return bytes.fromhex(d[off + 64:off + 64 + ln * 2]).decode("utf-8", "replace")


def dec_uint_array(data: str) -> list[int]:
    w = words(data)
    off = w[0] // 32
    return w[off + 1: off + 1 + w[off]]


# ---------------------------------------------------------------- amounts
def to_wei(x, decimals: int = 18) -> int:
    return int(Decimal(str(x).replace(",", ".")) * (10 ** decimals))


def fmt(v: int, decimals: int = 18, digits: int = 4) -> str:
    d = Decimal(v) / (Decimal(10) ** decimals)
    if d == 0:
        return "0"
    if abs(d) >= 1:
        s = f"{d:,.{digits}f}".replace(",", " ")
    else:  # tiny values: keep `digits` significant digits
        s = format(abs(d), "f")
        frac = s.split(".")[1]
        zeros = len(frac) - len(frac.lstrip("0"))
        s = ("-" if d < 0 else "") + "0." + frac[:zeros + digits]
    return s.rstrip("0").rstrip(".") if "." in s else s


def short(addr: str) -> str:
    return f"{addr[:6]}…{addr[-4:]}"


# ---------------------------------------------------------------- RPC
class Rpc:
    def __init__(self, urls: list[str], timeout: float = 6, max_inflight: int = 0):
        """max_inflight > 0 caps how many requests this client keeps open at once (background work)."""
        self.urls = urls
        self._sem = asyncio.Semaphore(max_inflight) if max_inflight else None
        self._i = 0
        self._ids = itertools.count(1)
        self.http = httpx.AsyncClient(timeout=timeout, headers={"user-agent": "elysium-trade-bot"})
        self._health_task = None

    def start_health(self, every: float = 5.0):
        """Keeps the freshest node first: public nodes can lag the chain by 10+ s, which means stale quotes
        and late receipts. Among nodes within MAX_LAG blocks of the top, the fastest one wins."""
        if len(self.urls) < 2 or self._health_task:
            return

        async def loop():
            while True:
                try:
                    await self._pick_best()
                except Exception as ex:  # never let the health check kill anything
                    log.debug("rpc health: %s", ex)
                await asyncio.sleep(every)
        self._health_task = asyncio.get_running_loop().create_task(loop())

    async def _pick_best(self, max_lag: int = 10):
        async def probe(u):
            t = time.monotonic()
            try:
                return u, int(await asyncio.wait_for(self._one(u, "eth_blockNumber", []), 3), 16), time.monotonic() - t
            except Exception:
                return u, -1, 99.0
        res = await asyncio.gather(*(probe(u) for u in self.urls))
        for u, h, _ in res:
            if h > 0:
                _HEIGHTS[u] = h
        top = max(h for _, h, _ in res)
        if top < 0:
            return
        fresh = sorted((dt, u) for u, h, dt in res if h >= top - max_lag)
        best = fresh[0][1]
        i = self.urls.index(best)
        if i != self._i:
            log.info("rpc: switching to %s (top block %s)", best, top)
            self._i = i

    async def req(self, method: str, params: list | None = None):
        if self._sem is None:
            return await self._req(method, params)
        async with self._sem:
            return await self._req(method, params)

    async def _logs(self, params: list):
        """eth_getLogs spread over all nodes: the one free soonest under its per-minute pace that has already
        seen the requested blocks; a node answering 'too many' cools down for a minute, the next one is used."""
        global _LOG_LOCK
        if _LOG_LOCK is None:
            _LOG_LOCK = asyncio.Lock()
        flt = (params or [{}])[0]
        try:
            to = int(flt.get("toBlock", "0x0"), 16) if str(flt.get("toBlock", "")).startswith("0x") else 0
        except ValueError:
            to = 0
        gap = 60.0 / max(LOGS_PER_MIN, 1)
        last = None
        for _ in range(len(self.urls) * 2):
            async with _LOG_LOCK:
                now = time.monotonic()
                ok = [u for u in self.urls if _LOG_COOL.get(u, 0) <= now and (not to or _HEIGHTS.get(u, to) >= to)]
                if not ok:
                    ok = [u for u in self.urls if _LOG_COOL.get(u, 0) <= now] or list(self.urls)
                url = min(ok, key=lambda u: _LOG_NEXT.get(u, 0))
                wait = _LOG_NEXT.get(url, 0) - now
                _LOG_NEXT[url] = max(now, _LOG_NEXT.get(url, 0)) + gap
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                return await self._one(url, "eth_getLogs", params)
            except RpcError as ex:
                if _rate_limited(str(ex)):
                    _LOG_COOL[url] = time.monotonic() + 60
                    log.info("rpc %s: log searches rate-limited, pausing it for 60 s", url)
                    last = ex
                    continue
                raise
            except Exception as ex:
                _LOG_COOL[url] = time.monotonic() + 10
                last = ex
        raise RpcError(f"getLogs: all nodes busy ({last})")

    async def _req(self, method: str, params: list | None = None):
        if method == "eth_getLogs":
            return await self._logs(params or [])
        last = None
        for k in range(len(self.urls) * 2):
            url = self.urls[(self._i + k) % len(self.urls)]
            try:
                r = await self.http.post(url, json={"jsonrpc": "2.0", "id": next(self._ids), "method": method,
                                                    "params": params or []})
                r.raise_for_status()
                j = r.json()
                if "error" in j:
                    msg = j["error"].get("message", str(j["error"]))
                    data = j["error"].get("data")
                    # execution errors are real answers — don't rotate
                    raise RpcError(msg + (f" {data}" if data else ""))
                self._i = (self._i + k) % len(self.urls)
                return j["result"]
            except RpcError:
                raise
            except Exception as e:  # network / 5xx → next url
                last = e
                await asyncio.sleep(0.3)
        raise RpcError(f"RPC недоступен: {last}")

    async def _one(self, url: str, method: str, params: list):
        r = await self.http.post(url, json={"jsonrpc": "2.0", "id": next(self._ids), "method": method,
                                            "params": params})
        r.raise_for_status()
        j = r.json()
        if "error" in j:
            raise RpcError(j["error"].get("message", str(j["error"])))
        return j["result"]

    async def broadcast(self, raw: str) -> str:
        """Sends the signed tx to every RPC at once (nodes lag differently); returns the hash from the first OK."""
        tasks = [asyncio.ensure_future(self._one(u, "eth_sendRawTransaction", [raw])) for u in self.urls]
        for t in tasks:  # the slower nodes keep delivering after we return; don't warn about their errors
            t.add_done_callback(lambda f: f.cancelled() or f.exception())
        errors = []
        for fut in asyncio.as_completed(tasks):
            try:
                h = await fut
            except Exception as ex:
                errors.append(ex)
                continue
            if h:
                # let the other nodes finish delivering in the background
                return h
        msg = "; ".join(str(x) for x in errors)[:300]
        raise RpcError(msg or "RPC недоступен: broadcast failed")

    async def receipt_any(self, tx: str):
        """Receipt from whichever RPC already has it (None if none of them does yet)."""
        tasks = [asyncio.ensure_future(self._one(u, "eth_getTransactionReceipt", [tx])) for u in self.urls]
        try:
            for fut in asyncio.as_completed(tasks, timeout=4):
                try:
                    rc = await fut
                except Exception:
                    continue
                if rc:
                    return rc
        except asyncio.TimeoutError:
            pass
        finally:
            for t in tasks:
                t.cancel()
        return None

    async def nonce_max(self, addr: str) -> int:
        """Pending nonce, highest across nodes: a lagging node must not hand out a nonce that's already used."""
        tasks = [asyncio.ensure_future(self._one(u, "eth_getTransactionCount", [addr, "pending"])) for u in self.urls]
        done, pending = await asyncio.wait(tasks, timeout=0.4)  # don't wait for a slow node
        for t in pending:
            t.cancel()
        vals = [int(t.result(), 16) for t in done if not t.cancelled() and t.exception() is None]
        return max(vals) if vals else await self.nonce(addr)

    async def chain_id(self) -> int:
        return int(await self.req("eth_chainId"), 16)

    async def balance(self, addr: str) -> int:
        return int(await self.req("eth_getBalance", [addr, "latest"]), 16)

    async def call(self, to: str, data: str) -> str:
        return await self.req("eth_call", [{"to": to, "data": data}, "latest"])

    async def nonce(self, addr: str) -> int:
        return int(await self.req("eth_getTransactionCount", [addr, "pending"]), 16)

    async def code(self, addr: str) -> str:
        return await self.req("eth_getCode", [addr, "latest"])

    async def receipt(self, tx: str):
        return await self.req("eth_getTransactionReceipt", [tx])

    async def fees(self) -> tuple[int, int]:
        """(maxFeePerGas, maxPriorityFeePerGas)."""
        async def _tip():
            try:
                return int(await self.req("eth_maxPriorityFeePerGas"), 16)
            except RpcError:
                return 0
        blk, tip = await asyncio.gather(self.req("eth_getBlockByNumber", ["latest", False]), _tip())
        base = int(blk.get("baseFeePerGas", "0x0"), 16)
        return base * 2 + tip, tip


class Sender:
    """Signs and sends type-2 txs; one tx at a time per address (nonce safety)."""

    def __init__(self, rpc: Rpc, chain_id: int):
        self.rpc, self.chain_id = rpc, chain_id
        self._locks: dict[str, asyncio.Lock] = {}
        self._next_nonce: dict[str, int] = {}  # our own count: nodes see new txs late
        self.on_sent = None  # optional async callback(hash) right after broadcast (set per call via send())

    def lock(self, addr: str) -> asyncio.Lock:
        return self._locks.setdefault(addr.lower(), asyncio.Lock())

    async def estimate(self, acct, to: str, data: str = "0x", value: int = 0) -> int:
        g = await self.rpc.req("eth_estimateGas", [{"from": acct.address, "to": to, "data": data,
                                                     "value": hex(value)}])
        return int(int(g, 16) * 13 // 10) + 10_000

    async def gas_cost(self, acct, to: str, data: str = "0x", value: int = 0) -> int:
        gas = await self.estimate(acct, to, data, value)
        max_fee, _ = await self.rpc.fees()
        return gas * max_fee

    async def send(self, acct, to: str, data: str = "0x", value: int = 0, gas: int | None = None,
                   wait: float = 90, on_sent=None) -> dict:
        """Returns the receipt; raises TxFailed on revert. Caller should hold self.lock(addr)."""
        t_start = time.monotonic()

        async def _gas():
            return gas if gas is not None else await self.estimate(acct, to, data, value)
        # independent lookups in parallel: saves a few RPC round trips per trade
        gas, (max_fee, tip), nonce = await asyncio.gather(_gas(), self.rpc.fees(), self.rpc.nonce_max(acct.address))
        nonce = max(nonce, self._next_nonce.get(acct.address.lower(), 0))
        tx = {"chainId": self.chain_id, "nonce": nonce, "to": to_checksum_address(to),
              "value": value, "data": data, "gas": gas, "maxFeePerGas": max_fee, "maxPriorityFeePerGas": tip,
              "type": 2}
        t_prep = time.monotonic()
        signed = acct.sign_transaction(tx)
        raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
        raw_hex = "0x" + bytes(raw).hex().removeprefix("0x")
        try:
            h = await self.rpc.broadcast(raw_hex)
        except RpcError as ex:
            if "already known" not in str(ex).lower():
                raise
            h = "0x" + signed.hash.hex().removeprefix("0x")  # a node already has it
        self._next_nonce[acct.address.lower()] = nonce + 1
        if on_sent:
            try:
                await on_sent(h)
            except Exception as ex:
                log.debug("on_sent: %s", ex)
        t0 = time.monotonic()
        wall_sent = time.time()
        t_sub = t0 - t_prep
        while time.monotonic() - t0 < wait:
            await asyncio.sleep(0.25)
            rc = await self.rpc.receipt_any(h)
            if rc:
                if int(rc["status"], 16) != 1:
                    raise TxFailed("транзакция отклонена сетью (revert)", h)
                rc["_hash"] = h
                now = time.monotonic()
                # seconds: gas/fee/nonce lookups, signing + submit, waiting for the receipt
                rc["_timing"] = (t_prep - t_start, t_sub, now - t0)
                rc["_sent_at"] = wall_sent
                return rc
        self._next_nonce.pop(acct.address.lower(), None)  # maybe dropped: trust the nodes' nonce again
        raise TxFailed("нет подтверждения за 90 сек — проверьте в эксплорере", h)


def receipt_cost(rc: dict) -> int:
    return int(rc["gasUsed"], 16) * int(rc.get("effectiveGasPrice", "0x0"), 16)
