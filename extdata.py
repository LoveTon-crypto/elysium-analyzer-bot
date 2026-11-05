"""DexScreener pair, 15m candles (GeckoTerminal, else prices the bot recorded itself) and the chart image."""
from __future__ import annotations

import asyncio
import io
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

log = logging.getLogger("extdata")

DS = "https://api.dexscreener.com"
GECKO = "https://api.geckoterminal.com/api/v2"
TF = 900  # 15m candles


@dataclass
class Pair:
    url: str = ""
    chain: str = ""
    address: str = ""
    dex: str = ""
    vol24: float = 0.0
    txns24: int = 0
    chg: dict = field(default_factory=dict)   # h1 / h6 / h24 in %
    created: float = 0.0                       # seconds
    image: str = ""
    liq: float = 0.0
    mc: float = 0.0
    socials: dict = field(default_factory=dict)


class ExtData:
    def __init__(self, store, http: httpx.AsyncClient):
        self.store, self.http = store, http
        self._ds: dict[str, tuple[float, Pair | None]] = {}
        self._gt: dict[str, tuple[float, list]] = {}
        store.db.executescript("""
            CREATE TABLE IF NOT EXISTS prices (token TEXT, ts REAL, price REAL, mc REAL);
            CREATE INDEX IF NOT EXISTS prices_tok ON prices(token, ts);
            CREATE TABLE IF NOT EXISTS watch (token TEXT PRIMARY KEY, until REAL);
        """)
        store.db.commit()

    # ---------- own price history ----------
    def record(self, token: str, price: float, mc: float):
        """Saves a price point (at most one per minute per token); old points are dropped after 7 days."""
        token = token.lower()
        last = self.store.q("SELECT MAX(ts) FROM prices WHERE token=?", (token,))[0][0] or 0
        now = time.time()
        if price > 0 and now - last >= 55:
            self.store.q("INSERT INTO prices (token, ts, price, mc) VALUES (?,?,?,?)", (token, now, price, mc))
        if int(now) % 600 < 60:
            self.store.q("DELETE FROM prices WHERE ts<?", (now - 7 * 86400,))

    def history(self, token: str, since: float) -> list[tuple[float, float]]:
        return self.store.q("SELECT ts, price FROM prices WHERE token=? AND ts>=? ORDER BY ts", (token.lower(), since))

    def local_candles(self, token: str, n: int = 96) -> list[tuple]:
        pts = self.history(token, time.time() - n * TF)
        out, prev = [], None
        buckets: dict[int, list[float]] = {}
        for ts, p in pts:
            buckets.setdefault(int(ts // TF * TF), []).append(p)
        for b in sorted(buckets):
            v = buckets[b]
            o = prev if prev is not None else v[0]
            out.append((float(b), o, max(o, *v), min(o, *v), v[-1], 0.0))
            prev = v[-1]
        return out

    def changes(self, token: str, now_price: float) -> dict:
        """% change for 1h / 6h / 24h from recorded prices (only where history reaches that far)."""
        out = {}
        if now_price <= 0:
            return out
        first = self.store.q("SELECT MIN(ts) FROM prices WHERE token=?", (token.lower(),))[0][0]
        for k, sec in (("h1", 3600), ("h6", 21600), ("h24", 86400)):
            if not first or time.time() - first < sec * 0.9:
                continue
            r = self.store.q("SELECT price FROM prices WHERE token=? AND ts>=? ORDER BY ts LIMIT 1",
                             (token.lower(), time.time() - sec))
            if r and r[0][0]:
                out[k] = (now_price / r[0][0] - 1) * 100
        return out

    def watch(self, token: str, days: float):
        self.store.q("INSERT OR REPLACE INTO watch (token, until) VALUES (?, MAX(?, COALESCE((SELECT until FROM watch "
                     "WHERE token=?), 0)))", (token.lower(), time.time() + days * 86400, token.lower()))

    def watched(self) -> list[str]:
        return [r[0] for r in self.store.q("SELECT token FROM watch WHERE until>?", (time.time(),))]

    # ---------- DexScreener ----------
    async def pair(self, token: str) -> Pair | None:
        token = token.lower()
        c = self._ds.get(token)
        if c and time.monotonic() - c[0] < 60:
            return c[1]
        best = None
        try:
            r = await self.http.get(f"{DS}/latest/dex/tokens/{token}", timeout=8)
            pairs = (r.json() or {}).get("pairs") or [] if r.status_code < 400 else []
            mine = [p for p in pairs if ((p.get("baseToken") or {}).get("address") or "").lower() == token]
            if mine:
                p = max(mine, key=lambda x: float((x.get("liquidity") or {}).get("usd") or 0))
                tx = (p.get("txns") or {}).get("h24") or {}
                info = p.get("info") or {}
                soc = {}
                for s in info.get("socials") or []:
                    if s.get("type") in ("twitter", "telegram") and s.get("url"):
                        soc[s["type"]] = s["url"]
                for w in info.get("websites") or []:
                    if w.get("url"):
                        soc.setdefault("website", w["url"])
                best = Pair(url=p.get("url") or "", chain=p.get("chainId") or "", address=p.get("pairAddress") or "",
                            dex=p.get("dexId") or "", vol24=float((p.get("volume") or {}).get("h24") or 0),
                            txns24=int(tx.get("buys") or 0) + int(tx.get("sells") or 0),
                            chg={k: float(v) for k, v in (p.get("priceChange") or {}).items() if v is not None},
                            created=float(p.get("pairCreatedAt") or 0) / 1000, image=info.get("imageUrl") or "",
                            liq=float((p.get("liquidity") or {}).get("usd") or 0),
                            mc=float(p.get("marketCap") or p.get("fdv") or 0), socials=soc)
        except Exception as ex:
            log.info("dexscreener %s: %s", token, ex)
        self._ds[token] = (time.monotonic(), best)
        return best

    # ---------- GeckoTerminal candles ----------
    async def gecko_candles(self, token: str, pair: Pair | None) -> list[tuple]:
        token = token.lower()
        c = self._gt.get(token)
        if c and time.monotonic() - c[0] < 120:
            return c[1]
        rows: list[tuple] = []
        try:
            net, pool = (pair.chain, pair.address) if pair and pair.address else ("", "")
            if not pool:
                r = await self.http.get(f"{GECKO}/search/pools", params={"query": token}, timeout=8)
                for d in ((r.json() or {}).get("data") or []) if r.status_code < 400 else []:
                    base = (((d.get("relationships") or {}).get("base_token") or {}).get("data") or {}).get("id", "")
                    if base.lower().endswith("_" + token):
                        net = (((d.get("relationships") or {}).get("network") or {}).get("data") or {}).get("id", "")
                        pool = (d.get("attributes") or {}).get("address", "")
                        break
            if net and pool:
                r = await self.http.get(f"{GECKO}/networks/{net}/pools/{pool}/ohlcv/minute",
                                        params={"aggregate": 15, "limit": 96, "currency": "usd"}, timeout=10)
                ohlcv = ((((r.json() or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []) \
                    if r.status_code < 400 else []
                rows = sorted((tuple(float(x) for x in row[:6]) for row in ohlcv if len(row) >= 6), key=lambda x: x[0])
        except Exception as ex:
            log.info("geckoterminal %s: %s", token, ex)
        self._gt[token] = (time.monotonic(), rows)
        return rows

    async def candles(self, token: str, pair: Pair | None, decimals: int = 18, hype_usd: float = 0.0,
                      swaps=None) -> tuple[list[tuple], str]:
        """(candles, source): GeckoTerminal if it has the pool, else the pool's past swaps on-chain, else the
        prices the bot recorded itself — whichever covers the most time."""
        opts = [(await self.gecko_candles(token, pair), "GeckoTerminal")]
        if swaps is not None:
            try:
                opts.append((await asyncio.wait_for(swaps.candles(token, decimals, hype_usd), 90), "on-chain swaps"))
            except asyncio.TimeoutError:
                log.info("swaps %s: timeout", token)
        opts.append((self.local_candles(token), "on-chain"))
        rows, src = max(opts, key=lambda o: len(o[0]))
        return (rows, src) if len(rows) >= 3 else ([], "")


def _price_fmt(p: float) -> str:
    from scanner import price_fmt
    return price_fmt(p)


def render_chart(symbol: str, name: str, age: str, candles: list[tuple], cols: list[tuple[str, str, str]],
                 chg24: float | None, source: str) -> bytes | None:
    """15m candles with a header (ticker, MCAP, LIQ, VOL, 1H, 6H) — PNG, same look as the NEAR scanner."""
    if len(candles) < 3:
        return None
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle

    bg, fg, dim, up, down = "#15161c", "#e8e8ea", "#8a8d98", "#26a69a", "#ef5350"
    fig = plt.figure(figsize=(8, 4.8), dpi=130, facecolor=bg)
    head = fig.add_axes([0, 0.68, 1, 0.32], facecolor=bg)
    head.axis("off")
    head.set_ylim(0, 1)
    ax = fig.add_axes([0.04, 0.09, 0.84, 0.52], facecolor=bg)
    head.text(0.03, 0.84, symbol.upper()[:14], color=fg, fontsize=17, fontweight="bold", va="center")
    head.text(0.03, 0.62, f"{name[:40]}" + (f"  ·  {age}" if age else ""), color=dim, fontsize=10, va="center")
    if chg24 is not None:
        head.text(0.97, 0.84, f"{'↑' if chg24 >= 0 else '↓'}{abs(chg24):.2f}%", color=up if chg24 >= 0 else down,
                  fontsize=17, fontweight="bold", ha="right", va="center")
    for i, (label, val, kind) in enumerate(cols):
        color = up if kind == "up" else down if kind == "down" else fg
        x = 0.03 + i * 0.16
        head.text(x, 0.36, label, color=dim, fontsize=9, va="center")
        head.text(x, 0.17, val, color=color, fontsize=11, fontweight="bold", va="center")
    w = 0.6
    for i, (_, o, h, low, cl, _) in enumerate(candles):
        color = up if cl >= o else down
        ax.plot([i, i], [low, h], color=color, linewidth=0.9)
        ax.add_patch(Rectangle((i - w / 2, min(o, cl)), w, max(abs(cl - o), (h - low) * 0.02 or 1e-18),
                               facecolor=color, edgecolor=color))
    last = candles[-1][4]
    ax.axhline(last, color=down, linestyle=":", linewidth=0.8)
    ax.set_xlim(-1, len(candles))
    lo, hi = min(r[3] for r in candles), max(r[2] for r in candles)
    pad = (hi - lo) * 0.08 or hi * 0.05 or 1e-18
    ax.set_ylim(lo - pad, hi + pad)
    ax.yaxis.tick_right()
    ax.tick_params(colors=dim, labelsize=8, length=0)
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: _price_fmt(v)[1:]))
    step = max(1, len(candles) // 6)
    idx = list(range(0, len(candles), step))
    ax.set_xticks(idx)
    ax.set_xticklabels([datetime.fromtimestamp(candles[i][0], timezone.utc).strftime("%H:%M") for i in idx])
    for s in ax.spines.values():
        s.set_visible(False)
    ax.grid(color="#262833", linewidth=0.6)
    ax.text(0.0, 1.02, f"USD 15m · UTC · {source}", transform=ax.transAxes, color=dim, fontsize=8)
    ax.text(len(candles) + 0.3, last, " " + _price_fmt(last)[1:] + " ", color="white", fontsize=8,
            va="center", ha="left", bbox=dict(facecolor=down, edgecolor="none", pad=1.5), clip_on=False)
    fig.text(0.5, 0.4, "Elysium Scanner", color="#ffffff", alpha=0.05, fontsize=40, ha="center", va="center",
             fontweight="bold")
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=bg)
    plt.close(fig)
    return buf.getvalue()
