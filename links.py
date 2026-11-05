"""Launchpad labels, token pages and the buy / scan buttons under every post."""
from __future__ import annotations

import html
import re

from telegram import InlineKeyboardButton as B

import config as C

START_OK = re.compile(r"^[A-Za-z0-9_-]{1,64}$")   # Telegram ?start= limit

# Venue (from dex.py) → launchpad label. Venues not listed here are plain DEX pools, not memepad launches.
_PAD_BY_NAME = (("Kleos", "KLEOS"), ("ElysPump", "ELYSPUMP"), ("Boxa", "BOXA"), ("ELYXA", "ELYXA"),
                ("Obol", "OBOL"), ("Montra", "MONTRA"), ("Stockium", "STOCKIUM"))
PAD_KINDS = {"kleos", "kleos4", "kpair", "elyxa", "obol", "boxa4", "ep4"}   # launch curves / launch hooks
NOT_PAD = {"KleosSwap", "Boxa V3", "Boxa Stocks", "Uniswap V3", "Uniswap V2", "ElysPump V2"}


def pad_of(venue) -> str | None:
    """Launchpad label of a dex.Venue, or None for a regular DEX pool."""
    if venue is None or venue.name in NOT_PAD:
        return None
    if venue.kind not in PAD_KINDS and "Stockium" not in venue.name:
        return None
    for prefix, label in _PAD_BY_NAME:
        if venue.name.startswith(prefix):
            return label
    return venue.name.split()[0].upper()


def pad_title(label: str) -> str:
    return {"KLEOS": "Kleos", "ELYSPUMP": "ElysPump", "BOXA": "Boxa", "OBOL": "Obol", "MONTRA": "Montra",
            "STOCKIUM": "Stockium", "ELYXA": "ELYXA"}.get(label, label.title())


def token_page(label: str, token: str) -> str:
    tpl = C.PAD_PAGES.get((label or "").upper())
    return tpl.format(token=token) if tpl else f"{C.EXPLORER}/token/{token}"


def addr_link(a: str) -> str:
    return f"{C.EXPLORER}/address/{a}"


def degen_link(token: str) -> str:
    """Degen bot opens the token card: ?start=r<ref>_<hex> (or _<hex> without a referral code)."""
    hx = token[2:].lower()
    for p in ((f"r{C.DEGEN_REF}_{hx}" if C.DEGEN_REF else ""), f"_{hx}"):
        if p and START_OK.match(p):
            return f"https://t.me/{C.DEGEN_BOT}?start={p}"
    return f"https://t.me/{C.DEGEN_BOT}"


def based_link(token: str) -> str:
    p = ""
    if C.BASED_TOKEN_PARAM:
        p = C.BASED_TOKEN_PARAM.format(ref=C.BASED_REF, token=token, hex=token[2:].lower())
        if not START_OK.match(p):
            p = ""
    p = p or C.BASED_REF
    return f"https://t.me/{C.BASED_BOT}?start={p}" if p else f"https://t.me/{C.BASED_BOT}"


def buy_row(token: str) -> list:
    return [B("⚡ Buy on Degen bot", url=degen_link(token)), B("🔵 Buy on Based", url=based_link(token))]


def scan_link(bot_username: str, token: str) -> str:
    return f"https://t.me/{bot_username}?start=s{token[2:].lower()}"


def e(s) -> str:
    return html.escape(str(s or ""))


def usd(x: float) -> str:
    x = float(x or 0)
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(x) >= div:
            return f"${x / div:.2f}".rstrip("0").rstrip(".") + suf
    if abs(x) >= 1 or x == 0:
        return f"${x:,.0f}" if abs(x) >= 100 else f"${x:.2f}"
    return f"${x:.6g}"


def amount(x: float) -> str:
    for div, suf in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(x) >= div:
            return f"{x / div:.2f}".rstrip("0").rstrip(".") + suf
    return f"{x:,.2f}".rstrip("0").rstrip(".")


def short(a: str) -> str:
    return f"{a[:6]}…{a[-4:]}"


def tree(rows: list[str]) -> list[str]:
    return [("└ " if i == len(rows) - 1 else "├ ") + r for i, r in enumerate(rows)]
