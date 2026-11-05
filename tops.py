"""/top (24h volume), /topmc (market cap), /topdex (hot right now).

DexScreener doesn't cover Elysium yet, so the bot counts volume itself: every swap it reads for big buys
(Uniswap v4 PoolManagers and v3 pools) is booked per token in HYPE by the hour. /topdex uses DexScreener's
data for tokens listed there and the bot's own on-chain activity for the rest, with the same trending-like score.
"""
from __future__ import annotations

import asyncio
import html
import logging
import math
import time

from evm import RpcError
from swaps import TRANSFER, V2_SWAP, V3_SWAP, V4_SWAP

log = logging.getLogger("tops")
NOT_MEMES = {"WHYPE", "HYPE", "KHYPE", "STHYPE", "KNTQ", "USDC", "USDT", "USDT0", "USDE", "USDH", "UETH", "UBTC",
             "WETH", "WBTC", "FEUSD", "USDXL"}


def _int(word: str) -> int:
    v = int(word, 16)
    return v - (1 << 256) if v >= 1 << 255 else v


class VolumeBook:
    def __init__(self, store, rpc, whype: set[str]):
        self.store, self.rpc, self.whype = store, rpc, whype
        self._v4: dict[str, str | None] = {}           # PoolManager:id → token (None = not a HYPE pool)
        self._v3: dict[str, tuple[str, int] | None] = {}  # pool → (token, index of the WHYPE side)
        self._pending: set[str] = set()
        store.db.executescript("""
            CREATE TABLE IF NOT EXISTS vol (token TEXT, hour INTEGER, hype REAL DEFAULT 0, buys INTEGER DEFAULT 0,
                                            sells INTEGER DEFAULT 0, PRIMARY KEY (token, hour));
        """)
        store.db.commit()

    def _book(self, token: str, hype: float, buy: bool):
        hour = int(time.time() // 3600)
        self.store.db.execute("INSERT INTO vol (token, hour, hype, buys, sells) VALUES (?,?,?,?,?) "
                              "ON CONFLICT(token, hour) DO UPDATE SET hype=hype+excluded.hype, buys=buys+excluded.buys, "
                              "sells=sells+excluded.sells", (token, hour, hype, int(buy), int(not buy)))

    async def add(self, logs: list[dict], pools_v3: dict):
        """Books a batch of Swap logs (pools_v3: bigbuys' cache pool → (token0, token1))."""
        for lg in logs:
            try:
                t = lg["topics"]
                d = lg["data"][2:]
                if t[0] == V4_SWAP:
                    key = lg["address"].lower() + ":" + t[1]
                    if key not in self._v4:
                        if key not in self._pending:
                            self._pending.add(key)
                            asyncio.get_running_loop().create_task(self._resolve_v4(key, lg))
                        continue
                    token = self._v4[key]
                    if not token:
                        continue
                    a0 = _int(d[:64])                       # HYPE delta of the swapper: negative = paid = buy
                    self._book(token, abs(a0) / 1e18, a0 < 0)
                elif t[0] == V2_SWAP:
                    pool = lg["address"].lower()
                    pair = pools_v3.get(pool)
                    if not pair:
                        continue
                    t0, t1 = pair
                    side = 0 if t0 in self.whype else 1 if t1 in self.whype else -1
                    if side < 0:
                        continue
                    a_in = [int(d[0:64], 16), int(d[64:128], 16)]
                    a_out = [int(d[128:192], 16), int(d[192:256], 16)]
                    hype_in, hype_out = a_in[side], a_out[side]
                    token = t1 if side == 0 else t0
                    self._book(token, max(hype_in, hype_out) / 1e18, hype_in > 0)
                elif t[0] == V3_SWAP:
                    pool = lg["address"].lower()
                    pair = pools_v3.get(pool)
                    if not pair:
                        continue
                    t0, t1 = pair
                    side = 0 if t0 in self.whype else 1 if t1 in self.whype else -1
                    if side < 0:
                        continue
                    token = t1 if side == 0 else t0
                    amt = _int(d[side * 64:(side + 1) * 64])  # pool's delta: positive = pool got HYPE = buy
                    self._book(token, abs(amt) / 1e18, amt > 0)
            except (KeyError, IndexError, ValueError):
                continue
        self.store.db.commit()
        if int(time.time()) % 3600 < 5:
            self.store.q("DELETE FROM vol WHERE hour<?", (int(time.time() // 3600) - 72,))

    async def _resolve_v4(self, key: str, lg: dict):
        """Which token a v4 pool trades: the ERC-20 in the same tx whose transfer equals the swap's amount1.
        If some ERC-20 transfer equals amount0 too, currency0 isn't native HYPE — not a HYPE pool."""
        try:
            d = lg["data"][2:]
            a0, a1 = abs(_int(d[:64])), abs(_int(d[64:128]))
            rc = await self.rpc.req("eth_getTransactionReceipt", [lg["transactionHash"]])
            token, hype_is_erc20 = None, False
            for x in (rc or {}).get("logs") or []:
                tt = x.get("topics") or []
                if len(tt) != 3 or tt[0] != TRANSFER:
                    continue
                v = int(x.get("data") or "0x0", 16)
                if v == a1 and x["address"].lower() not in self.whype:
                    token = x["address"].lower()
                elif v == a0 and x["address"].lower() not in self.whype:
                    hype_is_erc20 = True
            self._v4[key] = None if hype_is_erc20 else token
        except (RpcError, KeyError, IndexError, ValueError) as ex:
            log.debug("resolve %s: %s", key, ex)
        finally:
            self._pending.discard(key)

    def top_volume(self, hours: int = 24, limit: int = 30) -> list[tuple[str, float, int, int]]:
        since = int(time.time() // 3600) - hours + 1
        return self.store.q("SELECT token, SUM(hype), SUM(buys), SUM(sells) FROM vol WHERE hour>=? GROUP BY token "
                            "ORDER BY SUM(hype) DESC LIMIT ?", (since, limit))

    def stats(self, token: str, hours: int) -> tuple[float, int, int]:
        since = int(time.time() // 3600) - hours + 1
        r = self.store.q("SELECT SUM(hype), SUM(buys), SUM(sells) FROM vol WHERE token=? AND hour>=?", (token, since))[0]
        return float(r[0] or 0), int(r[1] or 0), int(r[2] or 0)


def hot_score(vol6: float, vol1: float, trades: int, buys: int, ch6: float, boosts: float = 0) -> float:
    ratio = buys / trades if trades else 0.5
    ch6 = max(-100.0, min(300.0, ch6))
    return (1.0 * math.log10(1 + vol6) + 0.6 * math.log10(1 + vol1) + 1.1 * math.log10(1 + trades)
            + 0.8 * (ratio - 0.5) + 0.004 * ch6 + 0.25 * math.log10(1 + boosts))


def usd(x: float) -> str:
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(x) >= div:
            return f"${x / div:.1f}{suf}"
    return f"${x:,.0f}"


TITLES = {"vol": "📊 <b>Top 10 Elysium · 24h volume</b>",
          "mc": "💰 <b>Top 10 Elysium memes · market cap</b>",
          "hot": "🔥 <b>Top 10 hot on Elysium</b>"}
SUBS = {"vol": "<i>swaps on Elysium v4 / v3 / v2 pools, counted by the bot</i>",
        "mc": "<i>memepad tokens · on-chain price</i>",
        "hot": "<i>ranked by 6h activity: volume, trades, buy pressure, price move</i>"}


def fmt(kind: str, rows: list[dict], link) -> str:
    lines = [TITLES[kind], SUBS[kind], ""]
    if not rows:
        lines.append("No data yet — the bot needs a little time to collect trades.")
    medals = ["🥇", "🥈", "🥉"] + [f"{i}." for i in range(4, 11)]
    for i, r in enumerate(rows[:10]):
        ch = r.get("change")
        chs = f" {'🟢' if ch >= 0 else '🔴'} {ch:+.0f}% 24h" if ch is not None else ""
        mc = usd(r["mc"]) if r.get("mc") else "—"
        main = (f"Vol <b>{usd(r['vol'])}</b> · MC {mc}" if kind == "vol" else
                f"MC <b>{usd(r['mc'])}</b> · Vol {usd(r['vol'])}" if kind == "mc" else
                f"MC {mc} · Vol 6h <b>{usd(r['vol6'])}</b> · {r.get('trades', 0)} txns")
        pad = r.get("pad_title") or ""
        lines.append(f'{medals[i]} <a href="{html.escape(link(r), quote=True)}"><b>${html.escape(r["symbol"][:12])}</b></a>'
                     f"{chs}" + (f" · {pad}" if pad else ""))
        lines.append(f"     {main}")
    lines.append("\n<i>Tap a token to open it · send its contract to scan</i>")
    return "\n".join(lines)
