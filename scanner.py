"""Token card for the scanner and for group alerts."""
from __future__ import annotations

import logging
import re

import httpx
from telegram import InlineKeyboardButton as B, InlineKeyboardMarkup

import config as C
import links
from links import e, usd  # noqa: F401  (calls.py imports usd from here)

log = logging.getLogger("scanner")
ADDR_IN_TEXT = re.compile(r"0x[0-9a-fA-F]{40}")


def extract_address(text: str) -> str | None:
    """0x… address from a message or a pad / explorer link."""
    m = ADDR_IN_TEXT.search(text or "")
    return m.group(0).lower() if m else None


def price_fmt(p: float) -> str:
    if p <= 0:
        return "—"
    if p >= 1:
        return f"${p:,.4f}".rstrip("0").rstrip(".")
    s = f"{p:.20f}".split(".")[1]
    zeros = len(s) - len(s.lstrip("0"))
    if zeros >= 4:
        sub = "".join("₀₁₂₃₄₅₆₇₈₉"[int(d)] for d in str(zeros))
        return f"$0.0{sub}{s[zeros:zeros + 4]}"
    return f"${p:.{zeros + 4}f}"


async def holders(http: httpx.AsyncClient, token: str) -> tuple[int | None, list[tuple[str, int]]]:
    """(holder count, top-10 [(address, raw amount)]) from a Blockscout-style explorer API; empty if unavailable."""
    if not C.EXPLORER_API:
        return None, []
    count, top = None, []
    try:
        r = await http.get(f"{C.EXPLORER_API}/tokens/{token}/counters", timeout=6)
        if r.status_code < 400:
            v = r.json().get("token_holders_count")
            count = int(v) if v is not None else None
        r = await http.get(f"{C.EXPLORER_API}/tokens/{token}/holders", timeout=6)
        if r.status_code < 400:
            for it in (r.json().get("items") or [])[:10]:
                a = ((it.get("address") or {}).get("hash") or "").lower()
                if a:
                    top.append((a, int(it.get("value") or 0)))
    except Exception as ex:
        log.debug("holders %s: %s", token, ex)
    return count, top


def card_lines(snap, row: dict | None, extra: list[str] | None = None) -> list[str]:
    """Short token block used in group alerts (MC levels, dips) and at the top of the scan card."""
    t = snap.token
    pad = snap.pad or (row or {}).get("pad") or ""
    head = f"🪙 <b>{e(t.name)}</b> (${e(t.symbol)})" + (f" · {e(links.pad_title(pad))}" if pad else "")
    lines = [head, f"<code>{t.address.lower()}</code>", ""]
    if snap.hype_usd:
        lines.append(f"💵 Price: <b>{price_fmt(snap.price_usd)}</b>")
        lines.append(f"📈 MC: <b>{usd(snap.mc)}</b>" + (f" · 💧 Liq: <b>{usd(snap.liq)}</b>" if snap.liq else ""))
    else:
        lines.append(f"💵 Price: <b>{snap.price_hype:.10g} HYPE</b>")
        lines.append(f"📈 MC: <b>{snap.mc_hype:,.0f} HYPE</b>")
    ath = (row or {}).get("ath") or 0
    if ath and ath > snap.mc * 1.05:
        lines.append(f"🔝 ATH: <b>{usd(ath)}</b> ({(snap.mc / ath - 1) * 100:+.0f}%)")
    lines += extra or []
    return lines


def alert_keyboard(token: str, bot_username: str, pad: str) -> InlineKeyboardMarkup:
    second = [B(f"📈 {links.pad_title(pad or 'ELYSIUM')}", url=links.token_page(pad, token))]
    if bot_username:
        second.append(B("🔎 Scan", url=links.scan_link(bot_username, token)))
    return InlineKeyboardMarkup([links.buy_row(token), second])
