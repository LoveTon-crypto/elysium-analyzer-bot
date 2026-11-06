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
        self.done = 0
        self.skip: dict[str, tuple[int, float]] = {}   # token → (misses in a row, don't re-check before ts)
        self.last_skipped = 0
        self.no_price = False
        self.last_res: dict = {}
        self.hot: dict[str, float] = {}    # token → ts of its last big buy: watched for HOT_HOURS, never backed off long
        # MC alerts: what happened to them (shown in /deploys) — they used to vanish without a trace
        self.mc_stats = {"sent": 0, "failed": 0, "silent": 0, "unbound": 0, "last": 0.0, "last_err": ""}
        self._mc_fail: dict[str, int] = {}  # token:level → failed posts (retried next pass, up to 5 times)

    def candidates(self) -> list[str]:
        now = time.time()
        # fresh launches (first 2 h) all; after that only tokens that are alive (MC ≥ TRACK_MIN_MC) within TRACK_DAYS,
        # or any age if they're big — dead memepad coins aren't re-quoted every minute
        rows = self.store.q("SELECT token FROM deploys WHERE posted<>-1 AND pad<>'' AND (ts>? OR (ts>? AND last_mc>=?) "
                            "OR last_mc>=?) ORDER BY block DESC LIMIT ?",
                            (now - 7200, now - C.TRACK_DAYS * 86400, C.TRACK_MIN_MC, C.TRACK_MIN_MC * 5, C.TRACK_MAX))
        hot_since = now - float(C.env("HOT_HOURS", "6")) * 3600
        self.hot = {t: ts for t, ts in self.hot.items() if ts > hot_since}
        out = list(self.hot)
        for r in rows:
            if r[0] not in out:
                out.append(r[0])
        vol = [r[0] for r in self.store.q("SELECT token FROM vol WHERE hour>=? GROUP BY token ORDER BY SUM(hype) DESC "
                                          "LIMIT 80", (int(now // 3600) - 23,))] if self._has_vol() else []
        for t in calls.recent_tokens(self.store.db, 30) + C.PAD_EXTRA + (self.ext.watched() if self.ext else []) + vol:
            if t not in out:
                out.append(t)
        return out

    def is_hot(self, token: str) -> bool:
        return token in self.hot

    def nudge(self, token: str):
        """A big buy just landed on this token: watch it, drop any back-off and check it right now
        (without the per-check cap — a hot token is worth the wait)."""
        token = token.lower()
        self.hot[token] = time.time()
        self.skip.pop(token, None)

        async def go():
            try:
                await asyncio.wait_for(self.check(token), 40)
            except Exception as ex:
                log.info("nudge %s: %s", token, ex)
        asyncio.get_running_loop().create_task(go())

    def _has_vol(self) -> bool:
        return bool(self.store.q("SELECT 1 FROM sqlite_master WHERE type='table' AND name='vol'"))

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
        # Tokens without a quotable pool (dead or never-launched Montra/Boxa coins) used to eat a 20 s timeout each,
        # every pass: with ~400 candidates one pass took 10-15 min and MC/dip alerts came late or not at all.
        # Now a token that doesn't answer is backed off (1, 2, 4 … 30 min) and each check is capped at TRACK_TIMEOUT.
        now = time.time()
        # No HYPE/USD price at all = MC can't be computed for anything: skip the pass instead of backing off every
        # token (that silenced all MC/dip alerts for hours when the price API didn't answer from the server).
        if not await self.market.hype_usd():
            self.no_price = True
            log.warning("tracker: no HYPE/USD price, pass skipped")
            return
        if self.no_price:                      # price is back: tokens backed off meanwhile get checked right away
            self.no_price = False
            self.skip.clear()
        all_tokens = self.candidates()
        tokens = [t for t in all_tokens if self.skip.get(t, (0, 0))[1] <= now]
        self.last_skipped = len(all_tokens) - len(tokens)
        sem = asyncio.Semaphore(int(C.env("TRACK_CONCURRENCY", "16")))
        limit = float(C.env("TRACK_TIMEOUT", "12"))
        self.last_count, self.done = len(tokens), 0

        young_since = now - 7200
        young = {r[0] for r in self.store.q("SELECT token FROM deploys WHERE ts>?", (young_since,))}

        alive = {r[0] for r in self.store.q("SELECT token FROM deploys WHERE last_mc>=?", (C.TRACK_MIN_MC,))}
        res = {"ok": 0, "nopool": 0, "timeout": 0, "err": 0}

        async def one(tok):
            async with sem:
                ok, why = False, "nopool"
                fresh = tok in young or tok in self.hot     # a slow quote on a live launch isn't a dead token
                try:
                    ok = await asyncio.wait_for(self.check(tok), limit * 2 if fresh else limit)
                except asyncio.TimeoutError:
                    why = "timeout"
                except Exception as ex:
                    why = "err"
                    log.debug("check %s: %s", tok, ex)
                res["ok" if ok else why] += 1
                if ok:
                    self.skip.pop(tok, None)
                else:
                    # Only "no pool" means dead. A slow node (timeout) used to back live tokens off for up to 30 min
                    # too — and their MC/dip alerts never came. Slow or once-alive tokens: retried within 2 min.
                    n = self.skip.get(tok, (0, 0))[0] + 1
                    cap = 120 if why != "nopool" or tok in alive else 1800
                    self.skip[tok] = (n, time.time() + (0 if fresh else min(60 * 2 ** (n - 1), cap)))
                self.done += 1
        await asyncio.gather(*(one(t) for t in tokens))
        self.last_tick = time.time()
        self.last_res = res
        log.info("tracker pass: %d checked (ok %d, no pool %d, timeout %d, err %d), %d backed off, %.0fs",
                 len(tokens), res["ok"], res["nopool"], res["timeout"], res["err"], self.last_skipped, time.time() - now)

    async def check(self, token: str) -> bool:
        """False if the token couldn't be quoted (the caller backs it off)."""
        row = self.store.deploy(token)
        # the venue the token was found on before: checked alone first (a full search costs dozens of RPC calls)
        snap = await self.market.try_snap(token, retries=0, venue=(row or {}).get("venue") or "")
        if not snap or not snap.hype_usd or snap.mc <= 0:
            return False
        now, mc = time.time(), snap.mc
        prev_mc = (row or {}).get("last_mc") or 0
        if row:
            self.store.q("UPDATE deploys SET last_mc=?, ath=MAX(ath, ?), venue=? WHERE token=?",
                         (mc, mc if (row["ts"] or 0) > now - C.ATH_DAYS * 86400 else 0, snap.route.venue.name, token))
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
            return True
        young = row and row["ts"] and now - row["ts"] < 7200
        # Silent only for a token we know nothing about yet (first look, old, no MC on record, no big buy): its
        # levels were passed before we watched it. "First look" used to mean first *successful* quote in this run,
        # so a token whose quotes kept timing out (backed off) and then ran got all its levels marked silently.
        await self.levels(token, snap, row, silent=first and not young and not prev_mc and token not in self.hot)
        await self.dip(token, snap, row, now)
        return True

    async def levels(self, token, snap, row, silent: bool):
        hit = [lvl for lvl in C.MCAP_LEVELS if snap.mc >= lvl and not self.store.seen(f"mc:{token}:{lvl}")]
        if not hit:
            return
        if silent:   # levels passed before we watched the token: remember them without posting
            for lvl in hit:
                self.store.mark(f"mc:{token}:{lvl}")
            self.mc_stats["silent"] += 1
            log.info("MC levels %s of %s marked silently (first look, old token)", hit, token)
            return
        # the highest level passed that has somewhere to go: a token that jumped 10K → 35K between checks used to
        # be posted only to '30k' — with just 10k/20k bound, every level was dropped without a word
        bound = [x for x in hit if self.store.topic(level_key(x))]
        if not bound:
            # nowhere to post: mark, so binding the topic later doesn't flood old alerts — but say so
            for x in hit:
                self.store.mark(f"mc:{token}:{x}")
            self.mc_stats["unbound"] += 1
            log.warning("MC alert %s (levels %s) not sent: no MC topic bound (/settopic mc …)", token, hit)
            return
        lvl = max(bound)
        key = level_key(lvl)
        age = ""
        if row and row.get("ts"):
            from deploys import ago
            age = f"🕒 Age: {ago(row['ts'])}"
        lines = [f"🎯 <b>${key.upper()} MARKET CAP</b>", ""] + scanner.card_lines(snap, row, [age] if age else [])
        msg = await self.post(key, "\n".join(lines), None, scanner.alert_keyboard(token, self.bot_username, snap.pad))
        fk = f"{token}:{lvl}"
        if msg is None:
            # the post failed (no rights in the channel, Telegram error …): the level stays unmarked and is retried
            n = self._mc_fail.get(fk, 0) + 1
            self._mc_fail[fk] = n
            self.mc_stats["failed"] += 1
            why = getattr(self.post, "last_err", "") or "post failed"
            self.mc_stats["last_err"] = f"{key} {token[:10]} (try {n}): {why}"
            log.warning("MC %s alert %s: post failed (try %d)", key, token, n)
            if n < 5:
                return
        self._mc_fail.pop(fk, None)
        for x in hit:
            self.store.mark(f"mc:{token}:{x}")
        if msg is not None:
            self.mc_stats["sent"] += 1
            self.mc_stats["last"] = time.time()
            log.info("MC %s alert %s (%.0f)", key, token, snap.mc)

    async def dip(self, token, snap, row, now):
        # v4 pools and pad curves don't report liquidity (0): filter tiny tokens by market cap instead
        if (snap.liq and snap.liq < C.DIP_MIN_LIQ) or (not snap.liq and snap.mc < C.DIP_MIN_MC):
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
