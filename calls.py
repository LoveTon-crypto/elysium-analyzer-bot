"""Calls: who first dropped a token in a group, PnL since the call, PnL card image and the callers leaderboard."""
from __future__ import annotations

import time
from dataclasses import dataclass

from scanner import usd

PERIODS = {"24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400, "all": 0}


@dataclass
class Call:
    chat_id: int
    token: str
    user: str          # display name
    mcap: float        # market cap at the call
    ts: float
    user_id: int
    username: str
    symbol: str
    peak: float        # highest market cap seen since the call
    last_mcap: float
    last_ts: float

    @property
    def sym(self) -> str:
        return self.symbol or self.token[:6]

    @property
    def x_now(self) -> float:
        return self.last_mcap / self.mcap if self.mcap else 0

    @property
    def x_peak(self) -> float:
        return max(self.peak, self.last_mcap) / self.mcap if self.mcap else 0


COLS = "chat_id, token, user, mcap, ts, user_id, username, symbol, peak, last_mcap, last_ts"


def migrate(db):
    """Old `calls` table had only (chat_id, token, user, mcap, ts) — add the new columns."""
    have = {r[1] for r in db.execute("PRAGMA table_info(calls)").fetchall()}
    for col, typ in (("user_id", "INTEGER DEFAULT 0"), ("username", "TEXT DEFAULT ''"), ("symbol", "TEXT DEFAULT ''"),
                     ("peak", "REAL DEFAULT 0"), ("last_mcap", "REAL DEFAULT 0"), ("last_ts", "REAL DEFAULT 0")):
        if col not in have:
            db.execute(f"ALTER TABLE calls ADD COLUMN {col} {typ}")
    db.execute("UPDATE calls SET peak=mcap WHERE peak=0")
    db.execute("UPDATE calls SET last_mcap=mcap WHERE last_mcap=0")
    db.commit()


def get_call(db, chat_id: int, token: str) -> Call | None:
    r = db.execute(f"SELECT {COLS} FROM calls WHERE chat_id=? AND token=?", (chat_id, token)).fetchone()
    return Call(*r) if r else None


def record_call(db, chat_id: int, token: str, user_id: int, name: str, username: str, symbol: str,
                mcap: float) -> Call | None:
    """First scan of a token in this chat becomes the call. Returns the call (existing or new)."""
    c = get_call(db, chat_id, token)
    if c:
        return c
    mcap = max(mcap, 0.0)   # no price yet → call is kept, entry MC is set by the first price seen
    now = time.time()
    db.execute(f"INSERT INTO calls ({COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
               (chat_id, token, name, mcap, now, user_id, username or "", symbol, mcap, mcap, now))
    db.commit()
    return get_call(db, chat_id, token)


def update_mcap(db, token: str, mcap: float, candles: list | None = None, supply: float = 0):
    """New market cap for every call of this token; candles (ts, o, h, l, c, v) catch peaks between checks."""
    if mcap <= 0:
        return
    now = time.time()
    db.execute("UPDATE calls SET mcap=?, peak=?, last_mcap=? WHERE token=? AND mcap<=0", (mcap, mcap, mcap, token))
    rows = db.execute("SELECT chat_id, ts, peak FROM calls WHERE token=?", (token,)).fetchall()
    for chat_id, ts, peak in rows:
        best = max(peak or 0, mcap)
        if candles and supply:
            highs = [r[2] for r in candles if r[0] >= ts]
            if highs:
                best = max(best, max(highs) * supply)
        db.execute("UPDATE calls SET last_mcap=?, last_ts=?, peak=? WHERE chat_id=? AND token=?",
                   (mcap, now, best, chat_id, token))
    db.commit()


def recent_tokens(db, days: int = 30) -> list[str]:
    return [r[0] for r in db.execute("SELECT DISTINCT token FROM calls WHERE ts>=?",
                                     (time.time() - days * 86400,)).fetchall()]


def ago(ts: float) -> str:
    s = max(0, time.time() - ts)
    if s < 3600:
        return f"{int(s // 60)}m"
    if s < 86400:
        return f"{int(s // 3600)}h"
    return f"{int(s // 86400)}d"


def xfmt(x: float) -> str:
    if x <= 0:
        return "—"
    if x >= 2:
        return f"{x:.1f}x"
    return f"{(x - 1) * 100:+.0f}%"


def caller_line(c: Call, now_mcap: float, esc) -> list[str]:
    who = f"@{c.username}" if c.username else c.user
    now = now_mcap or c.last_mcap
    if c.mcap <= 0:
        return [f"🎯 Called by <b>{esc(who)}</b> · {ago(c.ts)} ago",
                "     ⏳ Entry MC will be set at the first trade"]
    ch = (now / c.mcap - 1) * 100 if c.mcap else 0
    return [f"🎯 Called by <b>{esc(who)}</b> · {ago(c.ts)} ago",
            f"     {usd(c.mcap)} → {usd(now)}  <b>{ch:+.1f}%</b> · 🔝 ATH <b>{xfmt(c.x_peak)}</b>"]


def leaderboard(db, chat_id: int, period: str, esc) -> str:
    since = time.time() - PERIODS[period] if PERIODS[period] else 0
    rows = [Call(*r) for r in db.execute(f"SELECT {COLS} FROM calls WHERE chat_id=? AND ts>=? AND mcap>0",
                                         (chat_id, since)).fetchall()]
    title = {"24h": "24 hours", "7d": "7 days", "30d": "30 days", "all": "all time"}[period]
    if not rows:
        return f"🏆 <b>Top callers · {title}</b>\n\nNo calls yet. Drop a token contract in the chat to make a call."
    users: dict[int | str, list[Call]] = {}
    for c in rows:
        users.setdefault(c.user_id or c.user, []).append(c)
    ranked = sorted(users.values(), key=lambda cs: max(x.x_peak for x in cs), reverse=True)[:10]
    medals = ["🥇", "🥈", "🥉"] + [f"{i}." for i in range(4, 11)]
    lines = [f"🏆 <b>Top callers · {title}</b>", ""]
    for i, cs in enumerate(ranked):
        best = max(cs, key=lambda x: x.x_peak)
        who = f"@{best.username}" if best.username else best.user
        avg = sum(x.x_peak for x in cs) / len(cs)
        wins = sum(1 for x in cs if x.x_peak >= 2)
        lines.append(f"{medals[i]} <b>{esc(who)}</b> — best <b>{xfmt(best.x_peak)}</b> ${esc(best.sym)}")
        lines.append(f"     {len(cs)} call{'s' if len(cs) > 1 else ''} · avg {xfmt(avg)} · 2x+ hits {wins}")
    top = sorted(rows, key=lambda x: x.x_peak, reverse=True)[:5]
    lines += ["", "🔥 <b>Best calls</b>"]
    for c in top:
        who = f"@{c.username}" if c.username else c.user
        lines.append(f"${esc(c.sym)} · {usd(c.mcap)} → 🔝 {usd(max(c.peak, c.last_mcap))} "
                     f"<b>{xfmt(c.x_peak)}</b> · {esc(who)}")
    lines.append("\n<i>Ranked by ATH since the call.</i>")
    return "\n".join(lines)


def render_pnl(c: Call, name: str, now_mcap: float, candles: list | None, supply: float,
               chat_title: str = "") -> bytes:
    """Colorful degen PnL card (see pnl_art.py) with a random illustration."""
    import pnl_art
    now = now_mcap or c.last_mcap
    pts = [r[4] * supply for r in (candles or []) if r[0] >= c.ts - 900] if supply else []
    who = f"@{c.username}" if c.username else c.user
    return pnl_art.render(c.sym, name, who, ago(c.ts), c.mcap, now, max(c.peak, now), pts, chat_title)
