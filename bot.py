"""Elysium Analyzer — Telegram bot: token scanner, new-deploys channel for every Elysium memepad,
MC milestone / dip alerts in group topics, caller tracking with PnL cards. Port of the NEAR analyzer."""
from __future__ import annotations

import asyncio
import html
import logging
import re
import time

from telegram import InlineKeyboardButton as B, InlineKeyboardMarkup, InputMediaPhoto, LinkPreviewOptions, Update
from telegram.constants import ChatType, ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

import calls
import digest
import tops
import config as C
import links
import scanner
from bigbuys import MIN_HYPE, BigBuys
from deploys import DeployFeed, ago
from dex import DEFAULT_VENUES, Dex, parse_routers
from evm import Rpc
from extdata import ExtData, render_chart
from holders import HolderScanner, holder_lines
from swaps import SwapHistory
from links import e, usd
from market import Market, usd_status
from store import Store
from tracker import Tracker, level_key

logging.basicConfig(format="%(asctime)s %(name)s %(levelname)s %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("bot")
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

TOPICS = {level_key(v): f"Memepad token hit ${level_key(v).upper()} market cap" for v in C.MCAP_LEVELS}
TOPICS["dip"] = f"Memepad dip alert (−{C.DIP_PCT:g}% or more in {C.DIP_WINDOW_MIN} min)"
TOPICS["daily"] = f"Daily top-5 gainers / top-3 losers (24h), posted at {C.env('DIGEST_TIME', '18:00')}"
TOPICS["bigbuy"] = f"Big buys of {MIN_HYPE:g}+ HYPE (BIGBUY_HYPE in .env)"

store = Store(C.DB_PATH)
# Two connections to the nodes: commands people send (scan, tops, PnL) never wait behind background work —
# the deploys feed, tracker, big buys and volume run on their own client capped at BG_RPC_INFLIGHT requests.
rpc = Rpc(C.RPC_URLS)
rpc_bg = Rpc(C.RPC_URLS, max_inflight=int(C.env("BG_RPC_INFLIGHT", "12")))
VENUES = parse_routers(C.ROUTERS or DEFAULT_VENUES)
dex = Dex(rpc, VENUES)
market = Market(rpc, dex)
dex_bg = Dex(rpc_bg, parse_routers(C.ROUTERS or DEFAULT_VENUES))
market_bg = Market(rpc_bg, dex_bg)
# what either side learns (a token's venue, its v4 pool, names) is shared, so the other doesn't search again
for _attr in ("_venue_cache", "_v4keys", "_v4jobs", "_meta", "_supply"):
    setattr(dex_bg, _attr, getattr(dex, _attr))
feed = DeployFeed(store, rpc_bg, dex_bg, market_bg)
ext = ExtData(store, feed.http)
holders = HolderScanner(rpc, store, feed.http)
swaps = SwapHistory(rpc)
APP: Application | None = None


async def post(topic_key: str, text: str, image=None, kb=None):
    t = store.topic(topic_key)
    if not t or not APP:
        return None
    chat_id, thread_id = t
    kw = dict(chat_id=chat_id, parse_mode=ParseMode.HTML, reply_markup=kb)
    if thread_id:
        kw["message_thread_id"] = thread_id
    try:
        if image and len(re.sub(r"<[^>]+>", "", text)) <= 1024:
            return await APP.bot.send_photo(photo=image, caption=text, **kw)
        return await APP.bot.send_message(text=text, link_preview_options=NO_PREVIEW, **kw)
    except Exception as ex:
        log.warning("can't post to %s: %s", topic_key, ex)
        return None


tracker = Tracker(store, market_bg, post, ext)
big = BigBuys(store, rpc_bg, dex_bg, market_bg, post)
book = tops.VolumeBook(store, rpc_bg, big.whype)
big.book = book
big.tracker = tracker


# ---------------------------------------------------------------- owner
def owner_id() -> int | None:
    v = C.OWNER_ID or store.kv_get("owner")
    return int(v) if v and v.lstrip("-").isdigit() else None


def is_owner(update: Update) -> bool:
    return bool(update.effective_user) and update.effective_user.id == owner_id()


WELCOME = ("👋 <b>Elysium Analyzer</b>\n\n"
           "Paste an Elysium token contract (<code>0x…</code>) or a Kleos / ElysPump / Stockium link and get "
           "the analysis: price, market cap, liquidity, ATH, launchpad, dev wallet and their other deploys, "
           "top holders.\n\n"
           "🎯 In groups the first person to drop a contract gets the call — tap 📊 PnL under the card, "
           "and /callers shows the best callers.\n\n"
           "📊 /top — top 10 by 24h volume\n💰 /topmc — top 10 by market cap\n🔥 /topdex — hot right now")


def admin_help() -> str:
    return ("🛠 <b>Admin</b>\n\n"
            "🚀 <b>Deploys channel</b>: add me to the channel as admin, then here: "
            "<code>/setdeploys @channel</code>\n"
            "/deploys — status · <code>/deploytest 0x…</code> — preview a post here · "
            "<code>/deploypost 0x…</code> — post a token to the channel · /pads — launch contracts seen "
            "without a memepad pool\n\n"
            "📈 <b>Group alerts</b>: add me to a forum group as admin, open a topic and send "
            "<code>/settopic dip</code>\nKeys:\n" +
            "\n".join(f"<code>{k}</code> — {html.escape(v)}" for k, v in TOPICS.items()) +
            "\n/topics · /unsettopic &lt;key&gt; · /test &lt;key&gt; · /digest — post the daily top now")


# ---------------------------------------------------------------- scan card
def pct_fmt(x: float) -> str:
    return f"{'🟢' if x >= 0 else '🔴'} {x:+.1f}%"


def visible_len(s: str) -> int:
    return len(html.unescape(re.sub(r"<[^>]+>", "", s)))


async def build_card(update: Update, address: str, make_call: bool, slow: bool = True):
    """(text, keyboard, chart png | None) — or (None, None, None) if there's no pool.
    slow=False skips holders and the chart (they can take a while for an old token) for a first quick reply."""
    snap = await market.try_snap(address)
    if not snap:
        return None, None, None
    t = snap.token
    row = store.deploy(address)
    chat, u = update.effective_chat, update.effective_user
    if snap.mc > 0 and snap.hype_usd:
        calls.update_mcap(store.db, address, snap.mc)
        ext.record(address, snap.price_usd, snap.mc)
    ext.watch(address, C.TRACK_DAYS)          # scanned tokens get price history for the chart
    caller = calls.get_call(store.db, chat.id, address)
    if caller is None and make_call and chat.type != ChatType.PRIVATE and u and not u.is_bot:
        caller = calls.record_call(store.db, chat.id, address, u.id, u.first_name or "?", u.username or "",
                                   t.symbol, snap.mc if snap.hype_usd else 0)
    if slow:
        pair, hl, (candles, src) = await asyncio.gather(
            ext.pair(address), holders.get(address, t.supply), ext.candles(address, None, t.decimals, snap.hype_usd, swaps))
    else:
        pair, hl, candles, src = await ext.pair(address), None, [], ""

    pad = snap.pad or (row or {}).get("pad") or ""
    created = (row or {}).get("ts") or (pair.created if pair else 0)
    age = ago(created) if created else ""
    dev = (row or {}).get("dev") or ""
    pad_url = links.token_page(pad, address) if pad else ""
    title_url = pad_url or (pair.url if pair else "") or f"{C.EXPLORER}/token/{address}"
    head = f"${e(t.symbol.upper())}" + (f" · ⏱ {age}" if age else "") + (f" · {e(links.pad_title(pad))}" if pad else "")
    lines = [f'<a href="{e(title_url)}"><b>{e(t.name)}</b></a>', head, "", f"<code>{address}</code>", ""]
    chg = (pair.chg if pair and pair.chg else ext.changes(address, snap.price_usd))
    ath = max((row or {}).get("ath") or 0, snap.mc, max((c[2] for c in candles), default=0) * t.supply / 10 ** t.decimals
              if candles and snap.hype_usd else 0)
    if snap.hype_usd:
        lines.append(f"💵 PRICE <b>{scanner.price_fmt(snap.price_usd)}</b> · <i>{e(snap.route.venue.name)}</i>")
        lines.append(f"💰 MC <b>{usd(snap.mc)}</b>" + (f"   🔥 ATH <b>{usd(ath)}</b>" if ath > snap.mc * 1.02 else ""))
        liq = snap.liq or (pair.liq if pair else 0)
        if liq:
            lines.append(f"💧 LIQ <b>{usd(liq)}</b>")
    else:
        lines.append(f"💵 PRICE <b>{snap.price_hype:.10g} HYPE</b> · MC <b>{snap.mc_hype:,.0f} HYPE</b>")
    if pair:
        lines.append(f"🔄 VOL 24H <b>{usd(pair.vol24)}</b> · TXNS <b>{pair.txns24}</b>")
    if chg:
        lines.append(" · ".join(f"{k[1:].upper()} <b>{chg[k]:+.1f}%</b>" for k in ("h1", "h6", "h24") if k in chg))
    if dev:
        n = store.q("SELECT COUNT(*) FROM deploys WHERE dev=? AND posted<>-1", (dev,))[0][0]
        lines.append(f'🛠 Dev <a href="{links.addr_link(dev)}">{links.short(dev)}</a> · deploys {n}')
    if caller:
        lines += [""] + calls.caller_line(caller, snap.mc, e)
    lines.append("")
    lines += holder_lines(hl, links.addr_link, dev) if hl is not None else ["⏳ <i>Loading holders and chart…</i>"]

    hx = address[2:].lower()
    row2 = []
    if pair and pair.url:
        row2.append(B("📈 DexScreener", url=pair.url))
    if pad:
        row2.append(B(f"🚀 {links.pad_title(pad)}", url=pad_url))
    row2.append(B("👥 Holders", url=f"{C.EXPLORER}/token/{address}"))
    row3 = [B("🔄 Refresh", callback_data=f"rf:{hx}")]
    if caller:
        row3.append(B("📊 PnL", callback_data=f"pnl:{hx}"))
    kb = InlineKeyboardMarkup([links.buy_row(address), row2, row3])

    png = None
    if len(candles) >= 3:
        def k(v):
            return "up" if v >= 0 else "down"
        cols = [("MCAP", usd(snap.mc), ""), ("LIQ", usd(snap.liq or (pair.liq if pair else 0)) if (snap.liq or pair) else "—", ""),
                ("VOL", usd(pair.vol24) if pair else "—", ""), ("TXNS", str(pair.txns24) if pair else "—", "")]
        for key in ("h1", "h6"):
            if key in chg:
                cols.append((key[1:].upper() + "H", f"{'↑' if chg[key] >= 0 else '↓'}{abs(chg[key]):.2f}%", k(chg[key])))
        try:
            png = await asyncio.to_thread(render_chart, t.symbol, t.name, age, candles, cols, chg.get("h24"), src)
        except Exception as ex:
            log.info("chart %s: %s", address, ex)
    return "\n".join(lines), kb, png


async def send_card(update: Update, address: str, quiet: bool, make_call: bool):
    """Quick card first (price, MC, call), then the same card with holders and chart replaces it."""
    text, kb, _ = await build_card(update, address, make_call, slow=False)
    msg = update.effective_message
    if not text:
        if not quiet and dex.v4_searching(address):
            await msg.reply_html(f"⏳ Looking for the Uniswap V4 pool of <code>{e(address)}</code> from its trades — "
                                 "send it again in 20–30 seconds.")
        elif not quiet:
            await msg.reply_html(f"❌ No HYPE pool found for <code>{e(address)}</code> on Elysium "
                                 "(Kleos, ElysPump, Boxa, ELYXA, Obol, Montra, Stockium, Uniswap).")
        return
    first = await msg.reply_html(text, reply_markup=kb, link_preview_options=NO_PREVIEW)

    async def finish():
        try:
            text2, kb2, png = await asyncio.wait_for(build_card(update, address, False, slow=True), 120)
        except Exception as ex:
            log.info("full card %s: %s", address, ex)
            return
        if not text2:
            return
        try:
            if png and visible_len(text2) <= 1024:
                await msg.reply_photo(png, caption=text2, parse_mode=ParseMode.HTML, reply_markup=kb2)
                await first.delete()
            else:
                await first.edit_text(text2, parse_mode=ParseMode.HTML, reply_markup=kb2,
                                      link_preview_options=NO_PREVIEW)
                if png:
                    await msg.reply_photo(png)
        except Exception as ex:
            log.info("full card send %s: %s", address, ex)
    asyncio.get_running_loop().create_task(finish())


async def cmd_scan(update: Update, ctx):
    a = scanner.extract_address(" ".join(ctx.args))
    if not a:
        return await update.effective_message.reply_html("Usage: <code>/scan 0x…</code>")
    await send_card(update, a, quiet=False, make_call=True)


async def on_text(update: Update, ctx):
    a = scanner.extract_address(update.effective_message.text or "")
    if a:  # in groups stay quiet if it's not a token (could be a wallet)
        await send_card(update, a, quiet=update.effective_chat.type != ChatType.PRIVATE, make_call=True)


async def on_refresh(update: Update, ctx):
    q = update.callback_query
    text, kb, png = await build_card(update, "0x" + q.data[3:], False)
    if not text:
        return await q.answer("Pool not found right now.", show_alert=True)
    try:
        if q.message.photo and png and visible_len(text) <= 1024:
            await q.edit_message_media(InputMediaPhoto(png, caption=text, parse_mode=ParseMode.HTML), reply_markup=kb)
        elif q.message.photo:
            await q.edit_message_reply_markup(reply_markup=kb)
        else:
            await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb, link_preview_options=NO_PREVIEW)
    except Exception as ex:
        log.info("refresh: %s", ex)
    await q.answer("Updated")


async def on_pnl(update: Update, ctx):
    q = update.callback_query
    address = "0x" + q.data.split(":", 1)[1]
    call = calls.get_call(store.db, update.effective_chat.id, address)
    if not call:
        return await q.answer("No call for this token in this chat yet.", show_alert=True)
    snap = await market.try_snap(address)
    if snap and snap.mc > 0 and snap.hype_usd:
        calls.update_mcap(store.db, address, snap.mc)
        call = calls.get_call(store.db, update.effective_chat.id, address)
    if call.mcap <= 0:
        return await q.answer("The call is saved, but there's no price yet.", show_alert=True)
    await q.answer("Building PnL card…")
    now = snap.mc if snap and snap.hype_usd else call.last_mcap
    png = await asyncio.to_thread(calls.render_pnl, call, snap.token.name if snap else "", now,
                                  tracker.points(address), 1, update.effective_chat.title or "")
    who = f"@{call.username}" if call.username else call.user
    pct = (now / call.mcap - 1) * 100
    cap = (f"📊 <b>${e(call.sym)}</b> called by <b>{e(who)}</b> {calls.ago(call.ts)} ago\n"
           f"{usd(call.mcap)} → {usd(now)}  <b>{pct:+.1f}%</b> · 🔝 ATH <b>{calls.xfmt(call.x_peak)}</b>")
    await q.message.reply_photo(png, caption=cap, parse_mode=ParseMode.HTML)


def callers_kb(active: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[B(("• " if p == active else "") + p.upper(), callback_data="top:" + p)
                                  for p in calls.PERIODS]])


async def cmd_callers(update: Update, ctx):
    if update.effective_chat.type == ChatType.PRIVATE:
        return await update.effective_message.reply_text("Use /callers in a group — calls are counted per group.")
    p = ctx.args[0].lower() if ctx.args and ctx.args[0].lower() in calls.PERIODS else "7d"
    await update.effective_message.reply_html(calls.leaderboard(store.db, update.effective_chat.id, p, html.escape),
                                              reply_markup=callers_kb(p), link_preview_options=NO_PREVIEW)


async def on_callers(update: Update, ctx):
    q = update.callback_query
    p = q.data.split(":", 1)[1]
    try:
        await q.edit_message_text(calls.leaderboard(store.db, update.effective_chat.id, p, html.escape),
                                  parse_mode=ParseMode.HTML, reply_markup=callers_kb(p), link_preview_options=NO_PREVIEW)
    except Exception as ex:
        log.info("callers: %s", ex)
    await q.answer()


# ---------------------------------------------------------------- /top /topmc /topdex
def _fast_row(token: str, hu: float) -> dict | None:
    """Row from what the bot already recorded (price/MC every minute, volume from swaps) — no node requests."""
    last = store.q("SELECT ts, price, mc FROM prices WHERE token=? ORDER BY ts DESC LIMIT 1", (token,))
    row = store.deploy(token) or {}
    fresh = last and time.time() - last[0][0] < 1800
    mc = (last[0][2] if fresh else 0) or (row.get("last_mc") or 0)
    sym = row.get("symbol") or ""
    sym_r = store.q("SELECT symbol FROM tok_sym WHERE token=?", (token,)) if not sym else []
    sym = sym or (sym_r[0][0] if sym_r else "")
    if not sym or sym.upper() in tops.NOT_MEMES:
        return None
    pad = row.get("pad") or ""
    v24, b24, s24 = book.stats(token, 24)
    v6, b6, s6 = book.stats(token, 6)
    v1, _, _ = book.stats(token, 1)
    ch = ext.changes(token, last[0][1]) if fresh else {}
    return dict(address=token, symbol=sym, mc=mc, vol=v24 * hu, vol6=v6 * hu, vol1=v1 * hu, trades=b6 + s6, buys=b6,
                change=ch.get("h24"), ch6=ch.get("h6", 0.0), pad=pad, pad_title=links.pad_title(pad) if pad else "", url="")


async def _ensure_symbols(tokens: list[str]):
    """Symbols for tokens the bot only saw in swaps (not deploys), cached in the database."""
    store.q("CREATE TABLE IF NOT EXISTS tok_sym (token TEXT PRIMARY KEY, symbol TEXT)")
    missing = [t for t in tokens if not (store.deploy(t) or {}).get("symbol")
               and not store.q("SELECT 1 FROM tok_sym WHERE token=?", (t,))]

    async def one(t):
        try:
            tk = await asyncio.wait_for(dex.token(t), 5)
            store.q("INSERT OR REPLACE INTO tok_sym (token, symbol) VALUES (?, ?)", (t, tk.symbol))
        except Exception:
            pass
    await asyncio.gather(*(one(t) for t in missing[:30]))


async def top_text(kind: str) -> str:
    hu = await market.hype_usd() or 0
    vol_tokens = [r[0] for r in book.top_volume(24, 40)]
    cands = vol_tokens if kind == "vol" else list(dict.fromkeys(vol_tokens + tracker.candidates()))
    await _ensure_symbols(cands)
    rows = [r for r in (_fast_row(t, hu) for t in cands) if r]
    if kind == "vol":
        rows.sort(key=lambda r: r["vol"], reverse=True)
    elif kind == "mc":
        rows = sorted([r for r in rows if r["pad"] and r["mc"] > 0], key=lambda r: r["mc"], reverse=True)
    else:
        rows = [r for r in rows if r["vol6"] > 0]
        for r in rows:
            r["score"] = tops.hot_score(r["vol6"], r["vol1"], r["trades"], r["buys"], r["ch6"] or 0)
        rows.sort(key=lambda r: r["score"], reverse=True)
    top = rows[:10]
    # MC for top tokens the tracker doesn't watch (seen only in swaps): one quick on-chain quote each
    need = [r for r in top if not r["mc"]]
    if need:
        snaps = await asyncio.gather(*(asyncio.wait_for(market.try_snap(r["address"], retries=0), 15) for r in need),
                                     return_exceptions=True)
        for r, sn in zip(need, snaps):
            if sn and not isinstance(sn, Exception):
                r["mc"] = sn.mc
                if not r["pad"] and sn.pad:
                    r["pad"], r["pad_title"] = sn.pad, links.pad_title(sn.pad)
    if kind == "hot":
        for r in top:
            p = await ext.pair(r["address"])
            if p:
                r["url"] = p.url

    def link(r):
        return r.get("url") or (links.token_page(r["pad"], r["address"]) if r["pad"] else f"{C.EXPLORER}/token/{r['address']}")
    return tops.fmt(kind, top, link)


def top_kb(active: str) -> InlineKeyboardMarkup:
    names = {"vol": "📊 Volume", "mc": "💰 MCap", "hot": "🔥 Hot"}
    return InlineKeyboardMarkup([[B(("• " if k == active else "") + v, callback_data="tl:" + k) for k, v in names.items()]])


async def send_top(update: Update, kind: str):
    msg = await update.effective_message.reply_html("⏳ Counting…")
    await msg.edit_text(await top_text(kind), parse_mode=ParseMode.HTML, reply_markup=top_kb(kind),
                        link_preview_options=NO_PREVIEW)


async def cmd_top(update: Update, ctx):
    await send_top(update, "vol")


async def cmd_topmc(update: Update, ctx):
    await send_top(update, "mc")


async def cmd_topdex(update: Update, ctx):
    await send_top(update, "hot")


async def on_toplist(update: Update, ctx):
    q = update.callback_query
    kind = q.data.split(":", 1)[1]
    await q.answer("Loading…")
    try:
        await q.edit_message_text(await top_text(kind), parse_mode=ParseMode.HTML, reply_markup=top_kb(kind),
                                  link_preview_options=NO_PREVIEW)
    except Exception as ex:
        log.info("toplist: %s", ex)


# ---------------------------------------------------------------- daily digest
async def digest_rows() -> list[dict]:
    """24h change of every watched memepad / called token from the prices the bot records each minute."""
    now = time.time()
    rows = []
    for token in tracker.candidates():
        last = store.q("SELECT ts, price, mc FROM prices WHERE token=? ORDER BY ts DESC LIMIT 1", (token,))
        old = store.q("SELECT ts, price FROM prices WHERE token=? AND ts>=? ORDER BY ts LIMIT 1", (token, now - 86400))
        if not last or not old or now - last[0][0] > 1800 or last[0][0] - old[0][0] < 20 * 3600 or not old[0][1]:
            continue
        mc = last[0][2] or 0
        if mc < digest.MIN_MC:
            continue
        row = store.deploy(token) or {}
        sym, name = row.get("symbol") or "", row.get("name") or ""
        if not sym:
            try:
                tk = await dex.token(token)
                sym, name = tk.symbol, tk.name
            except Exception:
                sym = links.short(token)
        rows.append(dict(address=token, symbol=sym, name=name, change=(last[0][1] / old[0][1] - 1) * 100,
                         fdv=mc, vol=0, liq=0, url="", icon=None, pad=row.get("pad") or ""))
    return rows


async def post_digest(_ctx=None) -> bool:
    if not store.topic("daily"):
        return False
    up, down = digest.pick(await digest_rows())
    if not up and not down:
        log.info("digest: nothing to post")
        return False
    for r in up + down:     # volume and picture from DexScreener when the token is listed there
        p = await ext.pair(r["address"])
        if p:
            r["vol"], r["icon"], r["url"] = p.vol24, p.image or None, p.url
    icons = await digest.fetch_icons(up + down)        # DexScreener pictures, if any
    for r in up + down:                                 # otherwise the picture from the token's launch tx
        if r["address"] not in icons:
            img = await feed.logo(r["address"])
            if img:
                icons[r["address"]] = img
    from datetime import datetime
    from zoneinfo import ZoneInfo
    date_str = datetime.now(ZoneInfo(C.env("DIGEST_TZ", "Europe/Minsk"))).strftime("%d %b %Y")
    png = await asyncio.to_thread(digest.render, up, down, icons, date_str)

    def link(r):
        return links.token_page(r["pad"], r["address"]) if r["pad"] else (r["url"] or f"{C.EXPLORER}/token/{r['address']}")
    await post("daily", digest.text(up, down, link), png)
    log.info("digest posted: %d up, %d down", len(up), len(down))
    return True


async def cmd_digest(update: Update, ctx):
    if not is_owner(update):
        return
    if not store.topic("daily"):
        return await update.effective_message.reply_html("Bind a topic first: <code>/settopic daily</code> inside the topic.")
    if not await post_digest():
        await update.effective_message.reply_text("Not enough price history yet: the bot needs ~24h of prices first.")


# ---------------------------------------------------------------- start / admin
async def cmd_start(update: Update, ctx):
    msg = update.effective_message
    if owner_id() is None and update.effective_chat.type == ChatType.PRIVATE and update.effective_user:
        store.kv_set("owner", str(update.effective_user.id))
        log.info("owner set to %s", update.effective_user.id)
    arg = ctx.args[0] if ctx.args else ""
    if re.fullmatch(r"s[0-9a-fA-F]{40}", arg):   # 🔎 Scan button from the channel / alerts
        return await send_card(update, "0x" + arg[1:].lower(), quiet=False, make_call=False)
    await msg.reply_html(WELCOME, link_preview_options=NO_PREVIEW)
    if is_owner(update) and update.effective_chat.type == ChatType.PRIVATE:
        await msg.reply_html(admin_help(), link_preview_options=NO_PREVIEW)


async def cmd_admin(update: Update, ctx):
    if is_owner(update):
        await update.effective_message.reply_html(admin_help(), link_preview_options=NO_PREVIEW)


async def cmd_settopic(update: Update, ctx):
    msg = update.effective_message
    if not is_owner(update):
        return
    keys = [k.lower() for k in ctx.args[:1]]
    if keys == ["mc"]:                                   # all market-cap levels at once
        keys = [level_key(v) for v in C.MCAP_LEVELS]
    if not keys or any(k not in TOPICS for k in keys):
        return await msg.reply_html("Usage: <code>/settopic dip</code> inside a group topic, or "
                                    "<code>/settopic 10k @channel</code> (<code>mc</code> = all MC levels)\n"
                                    "Keys: " + ", ".join(TOPICS))
    if len(ctx.args) > 1:                                # a channel: the owner can't send commands inside it
        try:
            chat = await ctx.bot.get_chat(ctx.args[1].strip())
            m = await ctx.bot.send_message(chat.id, "✅ Alerts connected.")
            await m.delete()
        except Exception as ex:
            return await msg.reply_html(f"❌ Can't post there: {e(ex)}\nAdd me to the channel as admin first.")
        for k in keys:
            store.set_topic(k, chat.id, None)
        return await msg.reply_html(f"✅ <b>{', '.join(keys)}</b> → <b>{e(chat.title or ctx.args[1])}</b>.")
    if update.effective_chat.type == ChatType.PRIVATE:
        return await msg.reply_html("Use this inside a topic of your group, or add the channel: "
                                    "<code>/settopic 10k @channel</code>")
    thread = msg.message_thread_id if msg.is_topic_message else None
    for k in keys:
        store.set_topic(k, update.effective_chat.id, thread)
    await msg.reply_html(f"✅ <b>{', '.join(keys)}</b> → {'this topic' if thread else 'this chat'}.")


async def cmd_unsettopic(update: Update, ctx):
    if is_owner(update) and ctx.args:
        store.q("DELETE FROM topics WHERE key=?", (ctx.args[0].lower(),))
        await update.effective_message.reply_html(f"Removed <code>{e(ctx.args[0])}</code>.")


async def cmd_topics(update: Update, ctx):
    if not is_owner(update):
        return
    rows = store.q("SELECT key, chat_id, thread_id FROM topics")
    await update.effective_message.reply_html(
        "\n".join(f"<code>{k}</code> → {c} / topic {t or 'General'}" for k, c, t in rows) or "No topics bound.")


async def cmd_test(update: Update, ctx):
    if not is_owner(update) or not ctx.args:
        return
    m = await post(ctx.args[0].lower(), f"🧪 Test post for <code>{e(ctx.args[0])}</code>")
    await update.effective_message.reply_text("✅ Sent" if m else "❌ Topic not bound or no rights")


async def cmd_setdeploys(update: Update, ctx):
    msg = update.effective_message
    if not is_owner(update):
        return
    if not ctx.args:
        return await msg.reply_html("Usage: <code>/setdeploys @channel</code> (or the channel id -100…)\n"
                                    "<code>/setdeploys off</code> — stop posting")
    v = ctx.args[0].strip()
    if v.lower() == "off":
        store.kv_set("deploy_channel", "off")
        return await msg.reply_text("Deploys channel turned off.")
    try:
        chat = await ctx.bot.get_chat(v)
        m = await ctx.bot.send_message(chat.id, "✅ Elysium deploys feed connected.")
        await m.delete()
    except Exception as ex:
        return await msg.reply_html(f"❌ Can't post there: {e(ex)}\nAdd me to the channel as admin first.")
    store.kv_set("deploy_channel", str(chat.id))
    await msg.reply_html(f"✅ New deploys will be posted to <b>{e(chat.title or v)}</b>.")


async def cmd_deploys(update: Update, ctx):
    if not is_owner(update):
        return
    s = feed.stats
    up = int(time.time() - s["t0"])
    lines = ["🚀 <b>Deploys feed</b>",
             f"Channel: <code>{e(feed.channel() or '— (use /setdeploys)')}</code>",
             f"Block: {feed.cursor} / head {feed.head} (lag {max(0, feed.head - feed.cursor)})",
             f"Uptime {up // 3600}h{up % 3600 // 60:02d}m · new tokens {s['tokens']} · posted {s['posted']} · "
             f"not memepad {s['skipped']} · errors {s['errors']}",
             f"Last post: {ago(s['last_post']) + ' ago' if s['last_post'] else '—'}",
             f"Big buys (≥{MIN_HYPE:g} HYPE): {big.stats['posted']} posted, last "
             f"{ago(big.stats['last']) + ' ago' if big.stats['last'] else '—'}",
             f"Tracker: {tracker.last_count} tokens, this pass {tracker.done}/{tracker.last_count} (backed off {tracker.last_skipped}), last full pass "
             f"{ago(tracker.last_tick) + ' ago' if tracker.last_tick else '—'}"]
    u = usd_status()
    lines.append(f"HYPE/USD: {u['v']:.3f} via {u['src'] or '—'}" +
                 (f" · ⚠️ {u['fails']} failed rounds: <code>{e(u['err'][:300])}</code>" if u["fails"] else ""))
    if s["last_err"]:
        lines.append(f"Last error: <code>{e(s['last_err'])}</code>")
    by_pad = store.q("SELECT pad, COUNT(*) FROM deploys WHERE posted=1 GROUP BY pad ORDER BY 2 DESC")
    if by_pad:
        lines.append("By pad: " + " · ".join(f"{e(p)} {n}" for p, n in by_pad))
    await update.effective_message.reply_html("\n".join(lines))


async def cmd_pads(update: Update, ctx):
    """Launch contracts (tx.to) of new tokens that had no memepad pool — candidates for PAD_FACTORIES."""
    if not is_owner(update):
        return
    if not feed.unknown:
        return await update.effective_message.reply_text("Nothing yet: every new token so far was on a known pad.")
    rows = [f'<a href="{links.addr_link(a)}">{a}</a> × {n} (e.g. <code>{feed.unknown_sample.get(a, "")}</code>)'
            for a, n in feed.unknown.most_common(15)]
    await update.effective_message.reply_html(
        "Launch contracts without a known memepad pool:\n" + "\n".join(rows) +
        "\n\nIf one of them is a memepad, add to .env: <code>PAD_FACTORIES=0x…=NAME</code>",
        link_preview_options=NO_PREVIEW)


async def _deploy_preview(update: Update, ctx, to_channel: bool):
    if not is_owner(update):
        return
    a = scanner.extract_address(" ".join(ctx.args))
    if not a:
        return await update.effective_message.reply_html("Usage: <code>/deploytest 0x…</code>")
    text, img, kb = await feed.build(a, ctx.bot.username)
    chat = feed.channel() if to_channel else update.effective_chat.id
    if not chat:
        return await update.effective_message.reply_text("Set the channel first: /setdeploys @channel")
    m = await feed.send(ctx.bot, chat, text, img, kb)
    if to_channel:
        await update.effective_message.reply_text("✅ Posted" if m else "❌ Failed, see /deploys")


async def cmd_deploytest(update: Update, ctx):
    await _deploy_preview(update, ctx, False)


async def cmd_deploypost(update: Update, ctx):
    await _deploy_preview(update, ctx, True)


# ---------------------------------------------------------------- run
async def on_startup(app: Application):
    global APP
    APP = app
    try:
        cid = await rpc.chain_id()
        if cid != C.CHAIN_ID:
            log.error("RPC chain id %s ≠ CHAIN_ID %s", cid, C.CHAIN_ID)
    except Exception as ex:
        log.error("RPC unavailable at start: %s", ex)
    await asyncio.gather(dex.init(), dex_bg.init())
    rpc.start_health()
    rpc_bg.start_health()
    app.create_task(feed.follow(app))
    app.create_task(feed.recheck(app))
    app.create_task(tracker.run(app))
    app.create_task(big.run(app))
    if C.BACKFILL_BLOCKS:
        app.create_task(feed.backfill(C.BACKFILL_BLOCKS))


async def on_shutdown(app: Application):
    store.kv_set("deploy_cursor", str(feed.cursor))
    await feed.http.aclose()


def main():
    if not C.BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is not set (put it in .env)")
    app = Application.builder().token(C.BOT_TOKEN).post_init(on_startup).post_shutdown(on_shutdown) \
        .concurrent_updates(True).build()
    for name, fn in [("start", cmd_start), ("help", cmd_start), ("admin", cmd_admin), ("scan", cmd_scan),
                     ("callers", cmd_callers), ("settopic", cmd_settopic), ("unsettopic", cmd_unsettopic),
                     ("topics", cmd_topics), ("test", cmd_test), ("setdeploys", cmd_setdeploys),
                     ("deploys", cmd_deploys), ("deploytest", cmd_deploytest), ("deploypost", cmd_deploypost),
                     ("pads", cmd_pads), ("digest", cmd_digest),
                     ("top", cmd_top), ("topmc", cmd_topmc), ("topdex", cmd_topdex)]:
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(CallbackQueryHandler(on_refresh, pattern=r"^rf:"))
    app.add_handler(CallbackQueryHandler(on_pnl, pattern=r"^pnl:"))
    app.add_handler(CallbackQueryHandler(on_callers, pattern=r"^top:"))
    app.add_handler(CallbackQueryHandler(on_toplist, pattern=r"^tl:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    import datetime as dt
    from zoneinfo import ZoneInfo
    hh, mm = (int(x) for x in C.env("DIGEST_TIME", "18:00").split(":"))
    app.job_queue.run_daily(post_digest, time=dt.time(hh, mm, tzinfo=ZoneInfo(C.env("DIGEST_TZ", "Europe/Minsk"))))
    log.info("Elysium Analyzer starting")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
