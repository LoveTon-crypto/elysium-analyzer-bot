"""Elysium Analyzer — Telegram bot: token scanner, new-deploys channel for every Elysium memepad,
MC milestone / dip alerts in group topics, caller tracking with PnL cards. Port of the NEAR analyzer."""
from __future__ import annotations

import asyncio
import html
import logging
import re
import time

from telegram import InlineKeyboardButton as B, InlineKeyboardMarkup, LinkPreviewOptions, Update
from telegram.constants import ChatType, ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

import calls
import config as C
import links
import scanner
from deploys import DeployFeed, ago
from dex import DEFAULT_VENUES, Dex, parse_routers
from evm import Rpc
from links import e, usd
from market import Market
from store import Store
from tracker import Tracker, level_key

logging.basicConfig(format="%(asctime)s %(name)s %(levelname)s %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("bot")
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

TOPICS = {level_key(v): f"Memepad token hit ${level_key(v).upper()} market cap" for v in C.MCAP_LEVELS}
TOPICS["dip"] = f"Memepad dip alert (−{C.DIP_PCT:g}% or more in {C.DIP_WINDOW_MIN} min)"

store = Store(C.DB_PATH)
rpc = Rpc(C.RPC_URLS)
dex = Dex(rpc, parse_routers(C.ROUTERS or DEFAULT_VENUES))
market = Market(rpc, dex)
feed = DeployFeed(store, rpc, dex, market)
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


tracker = Tracker(store, market, post)


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
           "and /callers shows the best callers.")


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
            "\n/topics · /unsettopic &lt;key&gt; · /test &lt;key&gt;")


# ---------------------------------------------------------------- scan card
async def build_card(update: Update, address: str, make_call: bool):
    snap = await market.try_snap(address)
    if not snap:
        return None, None
    row = store.deploy(address)
    chat, u = update.effective_chat, update.effective_user
    if snap.mc > 0 and snap.hype_usd:
        calls.update_mcap(store.db, address, snap.mc)
    caller = calls.get_call(store.db, chat.id, address)
    if caller is None and make_call and chat.type != ChatType.PRIVATE and u and not u.is_bot:
        caller = calls.record_call(store.db, chat.id, address, u.id, u.first_name or "?", u.username or "",
                                   snap.token.symbol, snap.mc if snap.hype_usd else 0)
    count, top = await scanner.holders(feed.http, address)
    extra = []
    t = snap.token
    if t.supply:
        extra.append(f"🏦 Supply: {links.amount(t.supply / 10 ** t.decimals)}")
    if snap.route:
        extra.append(f"🔁 Pool: {e(snap.route.venue.name)}")
    if row and row.get("ts"):
        extra.append(f"🕒 Age: {ago(row['ts'])}")
    dev = (row or {}).get("dev")
    if dev:
        n = store.q("SELECT COUNT(*) FROM deploys WHERE dev=? AND posted<>-1", (dev,))[0][0]
        extra.append(f'👤 Dev: <a href="{links.addr_link(dev)}">{links.short(dev)}</a> · deploys {n}')
    if count:
        extra.append(f"👥 Holders: {count:,}")
    if top and t.supply:
        rows = []
        for a, v in top:
            tag = " (dev)" if dev and a == dev else ""
            rows.append(f'<a href="{links.addr_link(a)}">{links.short(a)}</a> {v / t.supply * 100:.2f}%{tag}')
        extra.append("🐳 Top 10: " + " · ".join(rows[:10]))
    lines = scanner.card_lines(snap, row, extra)
    if caller:
        lines += [""] + calls.caller_line(caller, snap.mc, e)
    hx = address[2:].lower()
    kb = [links.buy_row(address),
          [B(f"📈 {links.pad_title(snap.pad or 'ELYSIUM')}", url=links.token_page(snap.pad or '', address)),
           B("🔄", callback_data=f"rf:{hx}")]]
    if caller:
        kb[1].append(B("📊 PnL", callback_data=f"pnl:{hx}"))
    return "\n".join(lines), InlineKeyboardMarkup(kb)


async def send_card(update: Update, address: str, quiet: bool, make_call: bool):
    text, kb = await build_card(update, address, make_call)
    msg = update.effective_message
    if not text:
        if not quiet:
            await msg.reply_html(f"❌ No HYPE pool found for <code>{e(address)}</code> on Elysium "
                                 "(Kleos, ElysPump, Boxa, ELYXA, Obol, Montra, Stockium, Uniswap).")
        return
    await msg.reply_html(text, reply_markup=kb, link_preview_options=NO_PREVIEW)


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
    text, kb = await build_card(update, "0x" + q.data[3:], False)
    if not text:
        return await q.answer("Pool not found right now.", show_alert=True)
    try:
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
    if update.effective_chat.type == ChatType.PRIVATE:
        return await msg.reply_text("Use this inside a topic of your group.")
    key = ctx.args[0].lower() if ctx.args else ""
    if key not in TOPICS:
        return await msg.reply_html("Usage: <code>/settopic dip</code>\nKeys: " + ", ".join(TOPICS))
    thread = msg.message_thread_id if msg.is_topic_message else None
    store.set_topic(key, update.effective_chat.id, thread)
    await msg.reply_html(f"✅ <b>{e(TOPICS[key])}</b> → {'this topic' if thread else 'this chat'}.")


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
        store.kv_set("deploy_channel", None)
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
             f"Tracker: {tracker.last_count} tokens, last run "
             f"{ago(tracker.last_tick) + ' ago' if tracker.last_tick else '—'}"]
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
    await dex.init()
    rpc.start_health()
    app.create_task(feed.follow(app))
    app.create_task(tracker.run(app))
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
                     ("pads", cmd_pads)]:
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(CallbackQueryHandler(on_refresh, pattern=r"^rf:"))
    app.add_handler(CallbackQueryHandler(on_pnl, pattern=r"^pnl:"))
    app.add_handler(CallbackQueryHandler(on_callers, pattern=r"^top:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    log.info("Elysium Analyzer starting")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
