"""Daily digest: top-5 gainers and top-3 losers over 24h among Elysium memepad tokens + tokens called in groups.

Text list with links (caption) + a table image with token icons, FDV, 24h volume and change.
"""
from __future__ import annotations

import html
import io
import os

import httpx
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

MIN_MC = float(os.environ.get("DIGEST_MIN_MC", "3000"))
N_UP = int(os.environ.get("DIGEST_GAINERS", "5"))
N_DOWN = int(os.environ.get("DIGEST_LOSERS", "3"))


def f(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def usd(x: float) -> str:
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(x) >= div:
            return f"${x / div:.2f}{suf}"
    return f"${x:,.0f}"


def pick(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    up = sorted([r for r in rows if r["change"] > 0], key=lambda r: r["change"], reverse=True)[:N_UP]
    down = sorted([r for r in rows if r["change"] < 0], key=lambda r: r["change"])[:N_DOWN]
    return up, down


def text(up: list[dict], down: list[dict], link) -> str:
    def row(r):
        return (f'• <a href="{html.escape(link(r), quote=True)}">${html.escape(r["symbol"])} {html.escape(r["name"][:24])}</a>'
                f' <b>{r["change"]:+.0f}%</b>')
    lines = ["🔥 <b>24h Price Ups & Downs</b>", "", "🟢 <b>Gainers</b>"]
    lines += [row(r) for r in up] or ["• —"]
    lines += ["", "🔴 <b>Losers</b>"]
    lines += [row(r) for r in down] or ["• — nobody dumped today"]
    return "\n".join(lines)


async def fetch_icons(rows: list[dict]) -> dict[str, bytes]:
    out = {}
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as c:
        for r in rows:
            if r.get("icon"):
                try:
                    resp = await c.get(r["icon"])
                    if resp.status_code == 200:
                        out[r["address"]] = resp.content
                except httpx.HTTPError:
                    pass
    return out


def render(up: list[dict], down: list[dict], icons: dict[str, bytes], date_str: str) -> bytes:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import patches as P
    from PIL import Image, ImageDraw

    bg, card, head, fg, dim, green, red, line = ("#0e1117", "#161b26", "#1e2533", "#eef1f6", "#8b93a7",
                                                 "#2ee59d", "#ff5a67", "#262d3d")
    rows_h = 0.62
    nu, nd = max(len(up), 1), max(len(down), 1)
    H = 1.2 + (rows_h * (nu + 1) + 0.7) + 0.3 + (rows_h * (nd + 1) + 0.7) + 0.3 + 0.5
    fig = plt.figure(figsize=(9, H), dpi=120, facecolor=bg)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, 9)
    ax.set_ylim(0, H)
    ax.axis("off")

    ax.text(0.45, H - 0.6, "ELYSIUM MEMES · 24H", color=fg, fontsize=22, fontweight="bold", va="center")
    ax.text(8.55, H - 0.6, date_str, color=dim, fontsize=12, ha="right", va="center")
    cols = [(0.45, "Token", "left"), (4.55, "MC", "right"), (6.4, "Volume", "right"), (8.45, "Change", "right")]

    def circle_icon(data: bytes):
        try:
            im = Image.open(io.BytesIO(data)).convert("RGBA").resize((96, 96))
        except Exception:
            return None
        mask = Image.new("L", (96, 96), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, 95, 95), fill=255)
        im.putalpha(mask)
        return im

    def section(y, title, rows, color):
        h = rows_h * (len(rows) + 1) + 0.7
        ax.add_patch(P.FancyBboxPatch((0.25, y - h), 8.5, h, boxstyle="round,pad=0,rounding_size=0.18",
                                      fc=card, ec=line, lw=1.2))
        ax.text(0.45, y - 0.35, title, color=color, fontsize=15, fontweight="bold", va="center")
        ty = y - 0.7 - rows_h / 2
        ax.add_patch(P.Rectangle((0.25, ty - rows_h / 2), 8.5, rows_h, fc=head, ec="none"))
        for x, label, ha in cols:
            ax.text(x, ty, label, color=dim, fontsize=11, fontweight="bold", ha=ha, va="center")
        for i, r in enumerate(rows or [None]):
            ry = ty - rows_h * (i + 1)
            ax.plot([0.25, 8.75], [ry + rows_h / 2, ry + rows_h / 2], color=line, lw=1)
            if r is None:
                ax.text(0.45, ry, "—", color=dim, fontsize=13, va="center")
                continue
            icon = circle_icon(icons[r["address"]]) if r["address"] in icons else None
            if icon is not None:
                ax.imshow(icon, extent=(0.45, 0.9, ry - 0.225, ry + 0.225), zorder=3)
            else:
                ax.add_patch(P.Circle((0.675, ry), 0.22, fc=color, alpha=0.25, zorder=3))
                ax.text(0.675, ry, r["symbol"][:1].upper(), color=fg, fontsize=12, fontweight="bold",
                        ha="center", va="center", zorder=4)
            ax.text(1.05, ry, r["symbol"][:10].upper(), color="#7cc4ff", fontsize=14, fontweight="bold", va="center")
            ax.text(4.55, ry, usd(r["fdv"]), color=fg, fontsize=13, ha="right", va="center")
            ax.text(6.4, ry, usd(r["vol"]) if r.get("vol") else "—", color=fg, fontsize=13, ha="right", va="center")
            arrow = "▲" if r["change"] >= 0 else "▼"
            ax.text(8.45, ry, f"{arrow} {r['change']:+.0f}%", color=color, fontsize=14, fontweight="bold",
                    ha="right", va="center")
        return y - h - 0.3

    y = H - 1.2
    y = section(y, "▲ Top Pumps", up, green)
    section(y, "▼ Top Dumps", down, red)
    ax.text(8.55, 0.3, "ELYSIUM ANALYZER", color=dim, fontsize=10, fontweight="bold", ha="right", va="center")
    ax.set_aspect("auto")

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=bg)
    plt.close(fig)
    return buf.getvalue()
