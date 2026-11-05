"""Watches memepad tokens every minute: ATH for the deploys feed, MC milestones and dips for group topics,
and the market cap of every call."""
from __future__ import annotations

import asyncio
import logging
import time

import calls
import config as C
import scanner
from links import e

log = logging.getLogger("tracker")


def level_key(v: int) -> str:
    return f"{v // 1000}k" if v % 1000 == 0 else str(v)


class Tracker:
    def __init__(self, store, market, post, ext=None):
        """post: async (topic_key, text, image, keyboard) → message | None; ext: extdata.ExtData (price history)."""
        self.store, self.market, self.post, self.ext = store, market, post, ext
        self.hist: dict[str, list[tuple[float, float]]] = {}   # token → [(ts, mc $)] for dips and PnL lines
        self.last_dip: dict[str, float] = {}
        self.known: set[str] = set()       # tokens seen in this run (first sighting doesn't alert old levels)
        self.bot_username = ""
        self.last_tick = 0.0
        self.last_count = 0

    def candidates(self) -> list[str]:
        now = time.time()
        rows = self.store.q("SELECT token FROM deploys WHERE posted<>-1 AND pad<>'' AND (ts>? OR last_mc>=?) "
                            "ORDER BY block DESC LIMIT ?", (now - C.TRACK_DAYS * 86400, C.TRACK_MIN_MC, C.TRACK_MAX))
        out = [r[0] for r in rows]
        for t in calls.recent_tokens(self.store.db, 30) + C.PAD_EXTRA + (self.ext.watched() if self.ext else []):
            if t not in out:
                out.append(t)
        return out

    def points(self, token: str) -> list[tuple]:
        """[(ts, 0, 0, 0, mc)] — candle-like rows for calls.render_pnl (supply = 1)."""
        return [(t, 0, 0, 0, v) for t, v in self.hist.get(token, [])]

    async def run(self, app):
        self.bot_username = app.bot.username
        await asyncio.sleep(15)
        while True:
            t0 = time.monotonic()
            try:
                await self.tick()
            except Exception:
                log.exception("tracker tick")
            await asyncio.sleep(max(5, C.TRACK_SECONDS - (time.monotonic() - t0)))

    async def tick(self):
        tokens = self.candidates()
        sem = asyncio.Semaphore(4)

        async def one(tok):
            async with sem:
                await self.check(tok)
        await asyncio.gather(*(one(t) for t in tokens))
        self.last_tick, self.last_count = time.time(), len(tokens)

    async def check(self, token: str):
        snap = await self.market.try_snap(token)
        if not snap or not snap.hype_usd or snap.mc <= 0:
            return
        now, mc = time.time(), snap.mc
        row = self.store.deploy(token)
        if row:
            self.store.q("UPDATE deploys SET last_mc=?, ath=MAX(ath, ?) WHERE token=?",
                         (mc, mc if (row["ts"] or 0) > now - C.ATH_DAYS * 86400 else 0, token))
            row["ath"] = max(row["ath"] or 0, mc)
        calls.update_mcap(self.store.db, token, mc)
        if self.ext:
            self.ext.record(token, snap.price_usd, mc)
        h = [(t, v) for t, v in self.hist.get(token, []) if now - t <= 7 * 86400][-2000:]
        h.append((now, mc))
        self.hist[token] = h
        first = token not in self.known
        self.known.add(token)
        if not (snap.pad or token in C.PAD_EXTRA):
            return
        young = row and row["ts"] and now - row["ts"] < 7200
        await self.levels(token, snap, row, silent=first and not young)
        await self.dip(token, snap, row, now)

    async def levels(self, token, snap, row, silent: bool):
        hit = [lvl for lvl in C.MCAP_LEVELS if snap.mc >= lvl and not self.store.seen(f"mc:{token}:{lvl}")]
        for lvl in hit:
            self.store.mark(f"mc:{token}:{lvl}")
        if not hit or silent:
            return
        lvl = max(hit)
        age = ""
        if row and row.get("ts"):
            from deploys import ago
            age = f"🕒 Age: {ago(row['ts'])}"
        lines = [f"🎯 <b>${level_key(lvl).upper()} MARKET CAP</b>", ""] + scanner.card_lines(snap, row, [age] if age else [])
        await self.post(level_key(lvl), "\n".join(lines), None,
                        scanner.alert_keyboard(token, self.bot_username, snap.pad))
        log.info("MC %s alert %s (%.0f)", level_key(lvl), token, snap.mc)

    async def dip(self, token, snap, row, now):
        if snap.liq < C.DIP_MIN_LIQ:
            return
        win = [(t, v) for t, v in self.hist.get(token, []) if now - t <= C.DIP_WINDOW_MIN * 60
               and t >= self.last_dip.get(token, 0)]
        if len(win) < 2:
            return
        peak_t, peak = max(win, key=lambda r: r[1])
        drop = (1 - snap.mc / peak) * 100 if peak else 0
        if drop < C.DIP_PCT or now - self.last_dip.get(token, 0) < C.DIP_COOLDOWN_MIN * 60:
            return
        self.last_dip[token] = now
        mins = max(1, int((now - peak_t) // 60))
        lines = [f"🩸 <b>DIP −{drop:.1f}%</b> in {mins}m", ""] + scanner.card_lines(snap, row)
        await self.post("dip", "\n".join(lines), None, scanner.alert_keyboard(token, self.bot_username, snap.pad))
        log.info("DIP alert %s -%.1f%% (%s)", token, drop, e(snap.token.symbol))
