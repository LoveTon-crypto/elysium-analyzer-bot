"""New tokens on every Elysium memepad → Telegram channel ("KLEOS DEPLOY!", "ELYSPUMP DEPLOY!" …).

How a launch is caught (same idea as the dev sniper in the Degen trade bot):
  • every second the bot reads ERC-20 mint logs (Transfer from 0x0) of the new blocks;
  • the first mint of a contract that had no code one block earlier = a new token;
  • dev = the wallet that sent the launch tx;
  • the pad is told by where the token trades (dex.py finds its pool: Kleos, ElysPump, Boxa, ELYXA, Obol, Montra,
    Stockium), or by the tx target listed in PAD_FACTORIES. Tokens with no memepad pool aren't posted
    (DEPLOY_POST_ALL=1 posts them too).
Post: picture, name / ticker / description / socials (from the launch tx, see meta.py), supply, MC, dev wallet,
dev HYPE balance, dev's share of supply, dev's earlier deploys and the best ATH among them, buy buttons.
"""
from __future__ import annotations

import asyncio
import collections
import logging
import re
import time

import httpx
from telegram import InlineKeyboardButton as B, InlineKeyboardMarkup, LinkPreviewOptions
from telegram.constants import ParseMode

import config as C
import links
import meta
from dex import NoPool
from evm import RpcError, encode
from links import e, tree, usd

log = logging.getLogger("deploys")
# launch contracts (tx target) of pads, from their docs / app configs
BUILTIN_FACTORIES = {
    "0x38d3ef3a06d18fa3286fb5deafe611be725fcd15": "MONTRA",    # Montra factory (montramarkets.xyz/docs/contracts)
    "0x00d4c904f74f74c6920c95e3ee807ab3c52dd205": "BOXA",      # Boxa launcher
}
RECHECK_MIN = 20       # tokens without a pool yet are re-checked for this long, then given up

TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ZERO_TOPIC = "0x" + "0" * 64
BALANCE_OF = "0x70a08231"
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
LP_SYMBOLS = {"UNI-V2", "UNIV2", "UNI-V3", "UNIV3", "WHYPE", "WETH", "WHYPE9", "SLP", "CAKE-LP"}


def is_lp(sym: str, name: str) -> bool:
    s = (sym or "").upper().replace(" ", "")
    return s in LP_SYMBOLS or s.endswith("-LP") \
        or "liquidity" in (name or "").lower() or "wrapped hype" in (name or "").lower()


def ago(ts: float) -> str:
    s = max(0, time.time() - (ts or 0))
    return f"{int(s)}s" if s < 60 else f"{int(s // 60)}m" if s < 3600 else \
        f"{int(s // 3600)}h" if s < 86400 else f"{int(s // 86400)}d"


class DeployFeed:
    def __init__(self, store, rpc, dex, market):
        self.store, self.rpc, self.dex, self.market = store, rpc, dex, market
        self.http = httpx.AsyncClient(timeout=12, headers={"user-agent": "elysium-analyzer-bot"})
        self.cursor = int(store.kv_get("deploy_cursor") or 0)
        self.head = 0
        self._seen: dict[str, float] = {}
        self._meta: dict[str, tuple[dict, bytes | None]] = {}
        self.sem = asyncio.Semaphore(8)
        self.unknown = collections.Counter()        # tx.to of new tokens without a memepad pool
        self.unknown_sample: dict[str, str] = {}
        self.stats = {"t0": time.time(), "tokens": 0, "posted": 0, "skipped": 0, "errors": 0, "last_err": "",
                      "last_post": 0.0}
        self.app = None

    # ---------- settings ----------
    def channel(self):
        v = self.store.kv_get("deploy_channel") or C.DEPLOY_CHANNEL
        if not v:
            return None
        return int(v) if v.lstrip("-").isdigit() else v

    # ---------- block follower ----------
    async def follow(self, app):
        self.app = app
        await asyncio.sleep(3)
        saved = time.monotonic()
        while True:
            try:
                self.head = int(await self.rpc.req("eth_blockNumber"), 16)
                if not self.cursor or self.head - self.cursor > C.DEPLOY_MAX_LAG:
                    if self.cursor:
                        log.warning("deploys: %s blocks behind, jumping to the head", self.head - self.cursor)
                    self.cursor = self.head
                if self.head > self.cursor:
                    frm, to = self.cursor + 1, min(self.head, self.cursor + 500)
                    logs = await self.rpc.req("eth_getLogs", [{"fromBlock": hex(frm), "toBlock": hex(to),
                                                               "topics": [TRANSFER, ZERO_TOPIC]}])
                    self._on_logs(logs or [], post=True)
                    self.cursor = to
                    if time.monotonic() - saved > 20:
                        self.store.kv_set("deploy_cursor", str(self.cursor))
                        saved = time.monotonic()
                    if to < self.head:
                        continue
            except Exception as ex:
                self.stats["errors"] += 1
                self.stats["last_err"] = f"{type(ex).__name__}: {ex}"[:200]
                log.warning("deploys follow: %s", ex)
                await asyncio.sleep(3)
            await asyncio.sleep(C.DEPLOY_POLL)

    def _on_logs(self, logs: list[dict], post: bool):
        now = time.monotonic()
        if len(self._seen) > 100_000:
            self._seen = {k: v for k, v in self._seen.items() if now - v < 3 * 86400}
        fresh: dict[str, tuple[str, int]] = {}
        for lg in logs:
            if len(lg.get("topics") or []) != 3:     # ERC-721 mints have 4 topics
                continue
            token = (lg.get("address") or "").lower()
            if not token or token in self._seen or token in fresh:
                continue
            fresh[token] = (lg.get("transactionHash") or "", int(lg.get("blockNumber") or "0x0", 16))
        for token, (txh, blk) in fresh.items():
            self._seen[token] = now
            if self.store.deploy(token):
                continue
            asyncio.get_running_loop().create_task(self._new(token, txh, blk, post))

    async def _new(self, token: str, txh: str, blk: int, post: bool):
        async with self.sem:
            try:
                await self._new_inner(token, txh, blk, post)
            except Exception as ex:
                self.stats["errors"] += 1
                self.stats["last_err"] = f"{token[:10]}: {type(ex).__name__}: {ex}"[:200]
                log.exception("deploy %s", token)

    async def _new_inner(self, token: str, txh: str, blk: int, post: bool):
        tx = await self.rpc.req("eth_getTransactionByHash", [txh])
        if not tx:
            return
        if blk > 1:
            try:  # contract already existed before this block → it's a mint of an old token, not a launch
                code = await self.rpc.req("eth_getCode", [token, hex(blk - 1)])
                if code and code not in ("0x", "0x0"):
                    return
            except RpcError:
                pass  # node without that state: carry on, the pool check below still filters
        dev, to = (tx.get("from") or "").lower(), (tx.get("to") or "").lower()
        await self._seed_v4_key(token, txh)
        try:
            tok = await self.dex.token(token)
        except (NoPool, RpcError):
            return
        if is_lp(tok.symbol, tok.name):
            return
        self.stats["tokens"] += 1
        snap, t0 = None, time.monotonic()
        wait = C.DEPLOY_ROUTE_WAIT if post else 0
        while True:
            snap = await self.market.try_snap(token)
            if snap or time.monotonic() - t0 >= wait:
                break
            await asyncio.sleep(3)
        if snap and snap.pad:
            self.learn_factory(to, snap.pad)
        pad = (snap.pad if snap else None) or C.PAD_FACTORIES.get(to) or self.known_launchers().get(to)
        ok = bool(pad) or C.POST_ALL
        if not pad:
            self.unknown[to or "?"] += 1
            self.unknown_sample[to or "?"] = token
        self.store.q("INSERT OR IGNORE INTO deploys (token, dev, pad, venue, factory, ts, block, tx, name, symbol, "
                     "posted, last_mc, ath) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (token, dev, pad or "", snap.route.venue.name if snap else "", to, time.time() if post else 0,
                      blk, txh, tok.name, tok.symbol, (0 if post else 2) if ok else -1,
                      snap.mc if snap else 0, snap.mc if snap else 0))
        if not ok or not post:
            self.stats["skipped"] += 0 if ok else 1
            return
        chat = self.channel()
        if not chat or not self.app:
            return
        text, img, kb = await self.build(token, self.app.bot.username, tx=tx, snap=snap)
        msg = await self.send(self.app.bot, chat, text, img, kb)
        self.store.q("UPDATE deploys SET posted=1, chat_id=?, msg_id=? WHERE token=?",
                     (str(chat), msg.message_id if msg else 0, token))
        self.stats["posted"] += 1
        self.stats["last_post"] = time.time()
        log.info("posted %s deploy %s ($%s) by %s", pad, token, tok.symbol, dev)
        if msg and C.DEPLOY_REFRESH > 0:
            await asyncio.sleep(C.DEPLOY_REFRESH)
            try:
                text2, _, kb2 = await self.build(token, self.app.bot.username, tx=tx)
                if text2 != text:
                    if msg.photo:
                        await msg.edit_caption(text2, parse_mode=ParseMode.HTML, reply_markup=kb2)
                    else:
                        await msg.edit_text(text2, parse_mode=ParseMode.HTML, reply_markup=kb2,
                                            link_preview_options=NO_PREVIEW)
            except Exception as ex:
                log.info("refresh %s: %s", token, ex)

    def known_launchers(self) -> dict[str, str]:
        """Launch contracts of the venues dex.py knows (Boxa launcher, pad routers, hooks) → pad label: a launch sent
        to one of them is a memepad deploy even if its pool doesn't answer a quote yet."""
        if getattr(self, "_launchers", None) is None:
            out = {}
            for v in getattr(self.dex, "venues", []):
                label = links.pad_of(v)
                if not label:
                    continue
                for a in (v.launcher, v.router, v.hook):
                    if a and a.startswith("0x") and a.lower() not in ("0x1b341fdd55d56f8e08409a2c25ddfad93eb03c1a",):
                        out.setdefault(a.lower(), label)       # (the shared Uniswap Universal Router isn't a pad)
            out.update(BUILTIN_FACTORIES)
            self._launchers = out
        learned = {r[0][3:]: r[1] for r in self.store.q("SELECT k, v FROM kv WHERE k LIKE 'pf:%'")}
        return {**self._launchers, **learned}

    def learn_factory(self, to: str, pad: str):
        """A launch contract whose token turned out to trade on a known pad is remembered as that pad's factory,
        so its next launches are labelled at once (no waiting for the pool)."""
        if to and pad and pad not in ("UNIV4",) and to not in self.known_launchers():
            self.store.kv_set("pf:" + to, pad)
            log.info("learned launch contract %s = %s", to, pad)

    async def _seed_v4_key(self, token: str, txh: str):
        """Launchpads that create a Uniswap v4 pool in the launch tx (Ascend, RONS v4 …): the pool's Initialize
        event is right in the receipt — hand its PoolKey to dex.py so the new token is priced at once."""
        try:
            from dex import _keccak
            from eth_utils import keccak
            topic = "0x" + keccak(text="Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)").hex().removeprefix("0x")
            rc = await self.rpc.req("eth_getTransactionReceipt", [txh])
            hypes = self.dex._hype_addrs()
            for lg in (rc or {}).get("logs") or []:
                t = lg.get("topics") or []
                if len(t) < 4 or t[0] != topic:
                    continue
                c0, c1 = "0x" + t[2][-40:].lower(), "0x" + t[3][-40:].lower()
                if token not in (c0, c1):
                    continue
                other = c1 if c0 == token else c0
                if other not in hypes:
                    continue
                d = lg["data"][2:]
                key = t[2][2:].rjust(64, "0") + t[3][2:].rjust(64, "0") + d[0:64] + d[64:128] + d[128:192]
                if _keccak(key) == t[1]:
                    self.dex._v4keys[token] = (time.monotonic(), (key, other))
                    self.dex._save_v4keys()
                    log.info("v4 pool from launch tx %s: hook 0x%s", token, key[-40:])
                    return
        except Exception as ex:
            log.debug("seed v4 %s: %s", token, ex)

    async def recheck(self, app):
        """Every minute: tokens launched in the last RECHECK_MIN minutes that had no pool when first seen get
        another try; once a memepad pool answers they're posted (late, but not lost)."""
        await asyncio.sleep(30)
        while True:
            try:
                since = time.time() - RECHECK_MIN * 60
                rows = self.store.q("SELECT token, factory, tx FROM deploys WHERE posted=-1 AND ts>? ORDER BY ts DESC "
                                    "LIMIT 40", (since,))
                for token, factory, txh in rows:
                    snap = await self.market.try_snap(token, retries=0)
                    pad = (snap.pad if snap else None) or self.known_launchers().get(factory or "")
                    if not pad:
                        continue
                    if snap and snap.pad:
                        self.learn_factory(factory or "", snap.pad)
                    self.store.q("UPDATE deploys SET pad=?, posted=0, venue=? WHERE token=?",
                                 (pad, snap.route.venue.name if snap else "", token))
                    self.unknown[factory or "?"] = max(0, self.unknown.get(factory or "?", 1) - 1)
                    chat = self.channel()
                    if chat:
                        tx = await self.rpc.req("eth_getTransactionByHash", [txh]) if txh else None
                        text, img, kb = await self.build(token, app.bot.username, tx=tx, snap=snap)
                        msg = await self.send(app.bot, chat, text, img, kb)
                        self.store.q("UPDATE deploys SET posted=1, chat_id=?, msg_id=? WHERE token=?",
                                     (str(chat), msg.message_id if msg else 0, token))
                        self.stats["posted"] += 1
                        self.stats["last_post"] = time.time()
                        log.info("late post %s deploy %s", pad, token)
            except Exception as ex:
                log.warning("recheck: %s", ex)
            await asyncio.sleep(60)

    # ---------- post ----------
    async def _token_meta(self, token: str, tx: dict | None, name: str, symbol: str) -> tuple[dict, bytes | None]:
        if token in self._meta:
            return self._meta[token]
        row = self.store.deploy(token) or {}
        try:
            if tx is None and row.get("tx"):
                tx = await self.rpc.req("eth_getTransactionByHash", [row["tx"]])
            rc = await self.rpc.req("eth_getTransactionReceipt", [tx["hash"]]) if tx else None
        except RpcError:
            rc = None
        m = await meta.resolve(self.http, (tx or {}).get("input") or "", (rc or {}).get("logs") or [], name, symbol)
        img = await meta.image_bytes(self.http, m.get("image", ""))
        if m.get("image") and row:
            self.store.q("UPDATE deploys SET image=? WHERE token=?", (m["image"], token))
        if len(self._meta) > 500:
            self._meta.clear()
        self._meta[token] = (m, img)
        return m, img

    async def logo(self, token: str) -> bytes | None:
        """Token picture from its launch tx (cached), for tables like the daily digest."""
        row = self.store.deploy(token) or {}
        if token in self._meta:
            return self._meta[token][1]
        if row.get("image"):
            return await meta.image_bytes(self.http, row["image"])
        if row.get("tx"):
            try:
                return (await self._token_meta(token, None, row.get("name") or "", row.get("symbol") or ""))[1]
            except Exception:
                return None
        return None

    async def build(self, token: str, bot_username: str, tx: dict | None = None, snap=None):
        token = token.lower()
        row = self.store.deploy(token) or {}
        snap = snap or await self.market.try_snap(token)
        tok = snap.token if snap else await self.dex.token(token)
        m, img = await self._token_meta(token, tx, tok.name, tok.symbol)
        pad = row.get("pad") or (snap.pad if snap else "") or "ELYSIUM"
        dev = row.get("dev") or ((tx or {}).get("from") or "").lower()
        supply = tok.supply / 10 ** tok.decimals if tok.supply else 0

        lines = [f"🚀 <b>{e(pad)} DEPLOY!</b>", "", "🏦 <b>Token Info</b>", f"<code>{tok.address.lower()}</code>"]
        info = [f"Name: <b>{e(tok.name)}</b>", f"Ticker: <b>${e(tok.symbol)}</b>"]
        desc = (m.get("description") or "").strip()
        if desc:
            info.append(f"Description: <i>{e(desc[:260])}{'…' if len(desc) > 260 else ''}</i>")
        if supply:
            info.append(f"Supply: {links.amount(supply)}")
        if snap and snap.hype_usd:
            mc_line = f"MC: <b>{usd(snap.mc)}</b>"
            if snap.liq:
                mc_line += f" · 💧 {usd(snap.liq)}"
            info.append(mc_line)
        socials = []
        for k, icon, title in (("website", "🌐", "Web"), ("twitter", "𝕏", "Twitter"), ("telegram", "✈️", "Telegram")):
            v = m.get(k) or ""
            if v.startswith("http"):
                socials.append(f'<a href="{e(v)}">{icon} {title}</a>')
        if socials:
            info.append(" · ".join(socials))
        lines += tree(info)

        lines += ["", "💰 <b>Dev Info</b>"]
        if dev:
            lines.append(f'<a href="{links.addr_link(dev)}"><code>{dev}</code></a>')
            prev = self.store.q("SELECT token, symbol, pad, ath FROM deploys WHERE dev=? AND token<>? AND posted<>-1 "
                                "ORDER BY block DESC", (dev, token))
            bal, held = await asyncio.gather(self._safe(self.rpc.balance(dev)),
                                             self._safe(self.rpc.call(token, encode(BALANCE_OF, dev))))
            rows = []
            if prev:
                names = [f'<a href="{e(links.token_page(p, t))}">{e(s or links.short(t))}</a>' for t, s, p, _ in prev[:5]]
                more = f" +{len(prev) - 5}" if len(prev) > 5 else ""
                rows.append(f"🕑 Deploys: {len(prev)} ({', '.join(names)}{more})")
                best = max(prev, key=lambda r: r[3] or 0)
                rows.append(f"🏆 Best ATH: <b>{usd(best[3])}</b> ({e(best[1])})" if best[3] else "🏆 Best ATH: —")
            else:
                rows.append("🆕 First deploy from this wallet")
            if isinstance(bal, int):
                rows.append(f"💎 Balance: <b>{bal / 1e18:,.3f} HYPE</b>")
            if isinstance(held, str) and supply:
                try:
                    pct = int(held, 16) / tok.supply * 100
                    rows.append(f"🪙 Dev holds: <b>{pct:.2f}%</b>")
                except (ValueError, ZeroDivisionError):
                    pass
            lines += tree(rows)
        else:
            lines.append("└ unknown")

        kb = [links.buy_row(token),
              [B(f"📈 {links.pad_title(pad)}", url=links.token_page(pad, token))]]
        if bot_username:
            kb[1].append(B("🔎 Scan", url=links.scan_link(bot_username, token)))
        if dev:
            kb[1].append(B("👤 Dev", url=links.addr_link(dev)))
        return "\n".join(lines), img, InlineKeyboardMarkup(kb)

    @staticmethod
    async def _safe(coro):
        try:
            return await coro
        except Exception:
            return None

    async def send(self, bot, chat_id, text: str, image, kb):
        kw = dict(chat_id=chat_id, parse_mode=ParseMode.HTML, reply_markup=kb)
        if image and len(re.sub(r"<[^>]+>", "", text)) <= 1024:
            try:
                return await bot.send_photo(photo=image, caption=text, **kw)
            except Exception as ex:
                log.info("deploy photo failed (%s), sending text", ex)
        try:
            return await bot.send_message(text=text, link_preview_options=NO_PREVIEW, **kw)
        except Exception as ex:
            self.stats["last_err"] = f"send: {ex}"[:200]
            log.warning("can't post deploy: %s", ex)
            return None

    # ---------- history ----------
    async def backfill(self, blocks: int):
        """Once: read mint logs of the last N blocks so earlier deploys count in the dev history (no posts)."""
        if blocks <= 0 or self.store.kv_get("backfill_done"):
            return
        await asyncio.sleep(10)
        head = int(await self.rpc.req("eth_blockNumber"), 16)
        start = max(1, head - blocks)
        log.info("backfill: blocks %s..%s", start, head)
        step = 2000
        b = start
        while b <= head:
            to = min(head, b + step - 1)
            try:
                logs = await self.rpc.req("eth_getLogs", [{"fromBlock": hex(b), "toBlock": hex(to),
                                                           "topics": [TRANSFER, ZERO_TOPIC]}])
            except RpcError as ex:
                if step > 100:
                    step //= 2
                    continue
                log.warning("backfill %s: %s", b, ex)
                logs = []
            self._on_logs(logs or [], post=False)
            b = to + 1
            while self.sem.locked():
                await asyncio.sleep(1)
            await asyncio.sleep(0.2)
        self.store.kv_set("backfill_done", "1")
        log.info("backfill done")
