"""Colorful degen PnL cards.

Each card picks a random illustration: winners get rockets, diamonds, money bags…; losers get skulls,
tombstones, falling knives… All artwork is drawn here in code (original, no third-party characters).
Own pictures can be added: put .png/.jpg files into pnl_images/win and pnl_images/loss next to bot.py —
the bot mixes them in randomly.
"""
from __future__ import annotations

import io
import math
import os
import random
from datetime import datetime, timezone

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import patches as P  # noqa: E402
from matplotlib import patheffects as pe  # noqa: E402

BASE = os.path.dirname(os.path.abspath(__file__))
IMG_DIRS = {True: os.path.join(BASE, "pnl_images", "win"), False: os.path.join(BASE, "pnl_images", "loss")}

WIN_TAGS = ["WAGMI", "SEND IT", "TO THE MOON", "DIAMOND HANDS", "GIGA CALL", "PRINTING", "WE ARE SO BACK",
            "ELYSIUM SZN", "UP ONLY", "CHAD ENTRY", "LFG", "ALPHA DELIVERED"]
LOSS_TAGS = ["REKT", "NGMI", "IT'S OVER", "BOUGHT THE TOP", "PAPER HANDS", "EXIT LIQUIDITY", "DOWN BAD",
             "CATCHING KNIVES", "RIP BOZO", "COPE", "SKILL ISSUE", "FUNDS ARE SAFU?"]

WIN = dict(bg1="#032b22", bg2="#0b0f2e", main="#2ef2a0", accent="#ffd23f", glow="#00ffa3", pink="#ff4fd8")
LOSS = dict(bg1="#3a0612", bg2="#12051f", main="#ff4d6d", accent="#ff9f1c", glow="#ff1744", pink="#b388ff")


def glow(lw=6, color="#000", alpha=0.6):
    return [pe.Stroke(linewidth=lw, foreground=color, alpha=alpha), pe.Normal()]


# ---------------- illustrations (drawn on a 0..1 square axes) ----------------
def art_rocket(ax, t):
    ax.add_patch(P.Circle((0.78, 0.8), 0.12, color="#f4f1de"))
    for cx, cy, r in ((0.74, 0.84, 0.025), (0.82, 0.76, 0.018), (0.8, 0.86, 0.012)):
        ax.add_patch(P.Circle((cx, cy), r, color="#d6d2bd"))
    rng = random.Random(7)
    for _ in range(40):
        ax.add_patch(P.Circle((rng.random(), rng.random()), rng.random() * 0.006 + 0.002, color="white", alpha=0.8))
    import matplotlib.transforms as T
    tr = T.Affine2D().rotate_deg_around(0.45, 0.45, -35) + ax.transData
    for w, h, c in ((0.16, 0.42, t["accent"]), (0.1, 0.3, "#ff6b35"), (0.05, 0.18, "#fff3b0")):
        ax.add_patch(P.Ellipse((0.45, 0.12), w, h, color=c, transform=tr, alpha=0.95))
    ax.add_patch(P.FancyBboxPatch((0.37, 0.25), 0.16, 0.42, boxstyle="round,pad=0,rounding_size=0.06",
                                  facecolor="#eef2f7", edgecolor="#9aa5b1", lw=2, transform=tr))
    ax.add_patch(P.Polygon([(0.37, 0.65), (0.53, 0.65), (0.45, 0.85)], color=t["main"], transform=tr))
    ax.add_patch(P.Polygon([(0.37, 0.3), (0.28, 0.2), (0.37, 0.42)], color=t["main"], transform=tr))
    ax.add_patch(P.Polygon([(0.53, 0.3), (0.62, 0.2), (0.53, 0.42)], color=t["main"], transform=tr))
    ax.add_patch(P.Circle((0.45, 0.52), 0.045, facecolor="#4cc9f0", edgecolor="#9aa5b1", lw=3, transform=tr))


def art_diamond(ax, t):
    top = [(0.2, 0.6), (0.35, 0.78), (0.65, 0.78), (0.8, 0.6)]
    tip = (0.5, 0.15)
    shades = ["#7df9ff", "#4cc9f0", "#b5f5ff", "#3a86ff"]
    for i in range(3):
        ax.add_patch(P.Polygon([top[i], top[i + 1], ((top[i][0] + top[i + 1][0]) / 2, 0.6)],
                               color=shades[i % 4], ec="white", lw=1.5))
    for i, (a, b) in enumerate(((0.2, 0.4), (0.4, 0.6), (0.6, 0.8))):
        ax.add_patch(P.Polygon([(a, 0.6), (b, 0.6), tip], color=shades[(i + 1) % 4], ec="white", lw=1.5))
    for sx, sy, s in ((0.15, 0.85, 0.06), (0.85, 0.3, 0.05), (0.82, 0.88, 0.04), (0.12, 0.35, 0.035)):
        sparkle(ax, sx, sy, s, t["accent"])


def sparkle(ax, x, y, s, color):
    ax.add_patch(P.Polygon([(x, y + s), (x + s * 0.25, y + s * 0.25), (x + s, y), (x + s * 0.25, y - s * 0.25),
                            (x, y - s), (x - s * 0.25, y - s * 0.25), (x - s, y), (x - s * 0.25, y + s * 0.25)],
                           color=color))


def art_moneybag(ax, t):
    ax.add_patch(P.Ellipse((0.5, 0.38), 0.6, 0.55, color="#c9a227", ec="#8a6d12", lw=3))
    ax.add_patch(P.Polygon([(0.38, 0.62), (0.62, 0.62), (0.7, 0.8), (0.3, 0.8)], color="#c9a227", ec="#8a6d12", lw=3))
    ax.add_patch(P.Rectangle((0.36, 0.6), 0.28, 0.05, color="#8a6d12"))
    ax.text(0.5, 0.36, "$", color="#3d2f00", fontsize=70, fontweight="bold", ha="center", va="center")
    for cx, cy in ((0.12, 0.15), (0.88, 0.2), (0.15, 0.75), (0.86, 0.72), (0.8, 0.9)):
        ax.add_patch(P.Circle((cx, cy), 0.06, color=t["accent"], ec="#b8860b", lw=2))
        ax.text(cx, cy, "N", color="#6b4f00", fontsize=14, fontweight="bold", ha="center", va="center")


def art_chad_coin(ax, t):
    ax.add_patch(P.Circle((0.5, 0.48), 0.36, color=t["accent"], ec="#b8860b", lw=5))
    ax.add_patch(P.Circle((0.5, 0.48), 0.3, fill=False, ec="#e0a800", lw=3))
    ax.add_patch(P.FancyBboxPatch((0.26, 0.5), 0.2, 0.1, boxstyle="round,pad=0,rounding_size=0.03", color="#111"))
    ax.add_patch(P.FancyBboxPatch((0.54, 0.5), 0.2, 0.1, boxstyle="round,pad=0,rounding_size=0.03", color="#111"))
    ax.plot([0.46, 0.54], [0.57, 0.57], color="#111", lw=5)
    ax.add_patch(P.Polygon([(0.28, 0.58), (0.33, 0.58), (0.29, 0.52)], color="white", alpha=0.7))
    ax.add_patch(P.Arc((0.5, 0.36), 0.3, 0.16, theta1=200, theta2=340, color="#5c3d00", lw=5))
    for i in range(14):
        a = i / 14 * 2 * math.pi
        ax.plot([0.5 + 0.4 * math.cos(a), 0.5 + 0.48 * math.cos(a)], [0.48 + 0.4 * math.sin(a), 0.48 + 0.48 * math.sin(a)],
                color=t["accent"], lw=4, alpha=0.8, solid_capstyle="round")


def art_green_candles(ax, t):
    rng = random.Random(3)
    y = 0.08
    for i in range(7):
        h = 0.06 + i * 0.03 + rng.random() * 0.03
        x = 0.1 + i * 0.12
        ax.plot([x, x], [y - 0.03, y + h + 0.04], color=t["main"], lw=2)
        ax.add_patch(P.Rectangle((x - 0.04, y), 0.08, h, color=t["main"]))
        y += h * 0.8
    ax.annotate("", xy=(0.95, 0.95), xytext=(0.1, 0.2),
                arrowprops=dict(arrowstyle="-|>,head_width=0.8,head_length=1.2", color=t["accent"], lw=6))


def art_skull(ax, t):
    for a, b in (((0.15, 0.1), (0.85, 0.35)), ((0.15, 0.35), (0.85, 0.1))):
        ax.plot([a[0], b[0]], [a[1], b[1]], color="#e8e8e8", lw=16, solid_capstyle="round")
    ax.add_patch(P.Ellipse((0.5, 0.6), 0.56, 0.52, color="#f1f1f1"))
    ax.add_patch(P.FancyBboxPatch((0.36, 0.28), 0.28, 0.16, boxstyle="round,pad=0,rounding_size=0.04", color="#f1f1f1"))
    ax.add_patch(P.Ellipse((0.39, 0.6), 0.14, 0.16, color="#1a0a12"))
    ax.add_patch(P.Ellipse((0.61, 0.6), 0.14, 0.16, color="#1a0a12"))
    ax.add_patch(P.Polygon([(0.5, 0.5), (0.46, 0.43), (0.54, 0.43)], color="#1a0a12"))
    for x in (0.42, 0.47, 0.53, 0.58):
        ax.plot([x, x], [0.29, 0.36], color="#1a0a12", lw=2)
    ax.add_patch(P.Circle((0.39, 0.6), 0.025, color=t["main"]))
    ax.add_patch(P.Circle((0.61, 0.6), 0.025, color=t["main"]))


def art_tombstone(ax, t):
    ax.add_patch(P.Rectangle((0, 0), 1, 0.15, color="#2d6a4f"))
    for i in range(30):
        x = i / 30
        ax.plot([x, x + 0.01], [0.15, 0.2], color="#40916c", lw=2)
    ax.add_patch(P.FancyBboxPatch((0.22, 0.12), 0.56, 0.62, boxstyle="round,pad=0,rounding_size=0.25",
                                  color="#8d99ae", ec="#5c677d", lw=4))
    ax.text(0.5, 0.56, "RIP", color="#2b2d42", fontsize=40, fontweight="bold", ha="center", va="center")
    ax.text(0.5, 0.36, "my bags", color="#2b2d42", fontsize=16, ha="center", va="center", style="italic")
    ax.plot([0.3, 0.38, 0.35, 0.44], [0.7, 0.62, 0.55, 0.48], color="#5c677d", lw=2)
    ax.add_patch(P.Circle((0.85, 0.85), 0.08, color="#e0e1dd", alpha=0.9))


def art_knife(ax, t):
    ax.add_patch(P.Polygon([(0.5, 0.15), (0.6, 0.55), (0.42, 0.55)], color="#dfe7ef", ec="#8d99ae", lw=2))
    ax.add_patch(P.Rectangle((0.4, 0.55), 0.22, 0.05, color="#6c757d"))
    ax.add_patch(P.FancyBboxPatch((0.45, 0.6), 0.12, 0.28, boxstyle="round,pad=0,rounding_size=0.04", color="#3d2c1e"))
    for x0 in (0.2, 0.8):
        ax.plot([x0, x0], [0.4, 0.95], color="white", lw=2, alpha=0.4, ls="--")
    rng = random.Random(5)
    y = 0.85
    for i in range(5):
        h = 0.08 + rng.random() * 0.05
        x = 0.08 + i * 0.2 if i < 2 else 0.68 + (i - 2) * 0.1
        ax.add_patch(P.Rectangle((x, y - h), 0.06, h, color=t["main"]))
        y -= h * 0.9
    for _ in range(6):
        ax.add_patch(P.Circle((0.5 + rng.uniform(-0.1, 0.1), 0.1 + rng.uniform(-0.05, 0.05)), 0.015, color=t["glow"]))


def art_red_crash(ax, t):
    xs = [0.05, 0.2, 0.3, 0.42, 0.5, 0.62, 0.75, 0.9]
    ys = [0.85, 0.9, 0.7, 0.75, 0.45, 0.5, 0.2, 0.02]
    ax.plot(xs, ys, color=t["main"], lw=8, solid_capstyle="round", path_effects=glow(14, t["glow"], 0.35))
    ax.plot([0, 1], [0.08, 0.08], color="#adb5bd", lw=3)
    for dx, dy in ((-0.08, -0.06), (0.06, -0.07), (-0.03, -0.08), (0.09, -0.02)):
        ax.plot([0.9, 0.9 + dx], [0.08, 0.08 + dy], color="#adb5bd", lw=2)
    ax.text(0.5, 0.96, "-99%", color=t["accent"], fontsize=20, fontweight="bold", ha="center", va="top", alpha=0.6)


def art_crying(ax, t):
    ax.add_patch(P.Circle((0.5, 0.5), 0.36, color="#ffd166", ec="#e0a800", lw=4))
    ax.add_patch(P.Arc((0.38, 0.58), 0.12, 0.08, theta1=0, theta2=180, color="#3d2c1e", lw=5))
    ax.add_patch(P.Arc((0.62, 0.58), 0.12, 0.08, theta1=0, theta2=180, color="#3d2c1e", lw=5))
    ax.add_patch(P.Arc((0.5, 0.3), 0.28, 0.16, theta1=20, theta2=160, color="#3d2c1e", lw=5))
    for x in (0.36, 0.64):
        ax.add_patch(P.Rectangle((x - 0.03, 0.2), 0.06, 0.36, color="#4cc9f0", alpha=0.85))
        ax.add_patch(P.Ellipse((x, 0.18), 0.12, 0.05, color="#4cc9f0", alpha=0.85))
    ax.text(0.5, 0.92, "sell?", color="white", fontsize=16, ha="center", style="italic", alpha=0.8)


import characters  # noqa: E402

# characters are the main pool; the simple icons stay as rare extras
WIN_ART = characters.WIN_CHARS * 3 + [art_rocket, art_diamond, art_moneybag]
LOSS_ART = characters.LOSS_CHARS * 3 + [art_skull, art_tombstone, art_red_crash]


def custom_images(win: bool) -> list[str]:
    d = IMG_DIRS[win]
    if not os.path.isdir(d):
        return []
    return [os.path.join(d, f) for f in os.listdir(d) if f.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))]


def draw_art(ax, win: bool, t, rng: random.Random):
    pics = custom_images(win)
    pool = [("fn", f) for f in (WIN_ART if win else LOSS_ART)] + [("img", p) for p in pics]
    kind, item = rng.choice(pool)
    ax.set_xlim(-0.05, 1.05)
    ax.set_ylim(-0.05, 1.05)
    ax.axis("off")
    if kind == "img":
        try:
            import matplotlib.image as mpimg
            img = mpimg.imread(item)
            ax.imshow(img, extent=(0, 1, 0, 1), aspect="equal")
            return
        except Exception:
            kind, item = "fn", rng.choice(WIN_ART if win else LOSS_ART)
    item(ax, t)


# ---------------- the card ----------------
def usd(x: float) -> str:
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(x) >= div:
            return f"${x / div:.1f}{suf}"
    return f"${x:,.0f}"


def render(symbol: str, name: str, who: str, called_ago: str, call_mc: float, now_mc: float, peak_mc: float,
           points: list[float] | None, chat_title: str = "", seed: int | None = None) -> bytes:
    rng = random.Random(seed)
    x_now = now_mc / call_mc if call_mc else 0
    pct = (x_now - 1) * 100
    win = pct >= 0
    t = WIN if win else LOSS
    x_peak = peak_mc / call_mc if call_mc else 0

    W, H = 12, 6.75
    fig = plt.figure(figsize=(W, H), dpi=110)
    bg = fig.add_axes([0, 0, 1, 1])
    bg.axis("off")
    # diagonal gradient + glow blob
    import numpy as np
    yy, xx = np.mgrid[0:1:200j, 0:1:356j]
    c1 = np.array(matplotlib.colors.to_rgb(t["bg1"]))
    c2 = np.array(matplotlib.colors.to_rgb(t["bg2"]))
    mix = np.clip((xx * 0.6 + (1 - yy) * 0.4), 0, 1)[..., None]
    img = c1 * (1 - mix) + c2 * mix
    blob = np.exp(-(((xx - 0.78) / 0.28) ** 2 + ((yy - 0.45) / 0.45) ** 2))[..., None]
    img = np.clip(img + blob * np.array(matplotlib.colors.to_rgb(t["glow"])) * 0.28, 0, 1)
    bg.imshow(img, extent=(0, 1, 0, 1), aspect="auto", origin="lower")
    bg.set_xlim(0, 1)
    bg.set_ylim(0, 1)

    # confetti for wins / rain streaks for losses
    for _ in range(70 if win else 45):
        x, y = rng.random(), rng.random()
        if win:
            bg.add_patch(P.Rectangle((x, y), 0.006, 0.012, angle=rng.random() * 90,
                                     color=rng.choice([t["main"], t["accent"], t["pink"], "#4cc9f0", "white"]),
                                     alpha=0.55))
        else:
            bg.plot([x, x - 0.01], [y, y - 0.05], color=t["pink"], lw=1, alpha=0.25)

    # faint price line along the bottom
    if points and len(points) >= 3:
        lo, hi = min(points), max(points)
        span = (hi - lo) or 1
        xs = [0.02 + 0.96 * i / (len(points) - 1) for i in range(len(points))]
        ys = [0.03 + 0.22 * (p - lo) / span for p in points]
        bg.plot(xs, ys, color=t["main"], lw=3, alpha=0.35)
        bg.fill_between(xs, ys, 0, color=t["main"], alpha=0.07)

    # header
    bg.text(0.045, 0.9, f"${symbol.upper()[:12]}", color="white", fontsize=40, fontweight="bold", va="center",
            path_effects=glow(8, t["glow"], 0.35))
    if name:
        bg.text(0.047, 0.81, name[:34], color="#cfd6e4", fontsize=15, va="center")
    pill = rng.choice(WIN_TAGS if win else LOSS_TAGS)
    bg.text(0.047, 0.725, f"  {pill}  ", color="#0b0f2e" if win else "white", fontsize=15, fontweight="bold",
            va="center", bbox=dict(boxstyle="round,pad=0.35", fc=t["accent"] if win else t["main"], ec="none"))

    # big number
    big = f"{x_now:.2f}x" if x_now >= 2 else f"{pct:+.1f}%"
    bg.text(0.042, 0.53, big, color=t["main"], fontsize=82, fontweight="bold", va="center",
            path_effects=glow(18, t["glow"], 0.45))
    bg.text(0.047, 0.385, f"called by {who}  ·  {called_ago} ago", color="white", fontsize=16, va="center",
            alpha=0.9)

    # stat chips
    chips = [("CALL", usd(call_mc), "white"), ("NOW", usd(now_mc), t["main"]),
             ("ATH", f"{usd(peak_mc)} · {x_peak:.1f}x", t["accent"])]
    x = 0.045
    for label, val, col in chips:
        w = 0.035 + 0.0125 * len(val)
        bg.add_patch(P.FancyBboxPatch((x, 0.2), w + 0.02, 0.11, boxstyle="round,pad=0,rounding_size=0.025",
                                      fc=(1, 1, 1, 0.08), ec=(1, 1, 1, 0.18), lw=1.2))
        bg.text(x + 0.012, 0.283, label, color="#aab3c5", fontsize=10, fontweight="bold", va="center")
        bg.text(x + 0.012, 0.235, val, color=col, fontsize=16, fontweight="bold", va="center")
        x += w + 0.035

    # illustration on the right
    art = fig.add_axes([0.55, 0.06, 0.43, 0.43 * W / H * 0.97])
    art.set_aspect("equal")
    draw_art(art, win, t, rng)

    # footer
    bg.text(0.955, 0.93, "ELYSIUM ANALYZER", color="white", fontsize=14, fontweight="bold", ha="right", va="center",
            alpha=0.85)
    foot = datetime.now(timezone.utc).strftime("%d %b %Y · %H:%M UTC") + (f"  ·  {chat_title[:30]}" if chat_title else "")
    bg.text(0.045, 0.06, foot, color="white", fontsize=10, alpha=0.45, va="center")

    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()


# ---------------- wallet PnL card ----------------
def money(x: float, sign: bool = False) -> str:
    s = "+" if sign and x >= 0 else ("-" if x < 0 else "")
    x = abs(x)
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if x >= div:
            return f"{s}${x / div:.2f}".rstrip("0").rstrip(".") + suf
    return f"{s}${x:,.0f}" if x >= 100 else f"{s}${x:,.2f}"


def render_wallet(wallet: str, period_label: str, pnl: float, pct: float, bought: float, sold: float,
                  hold: float, wins: int, tokens: int, best: str = "", points: list[float] | None = None,
                  seed: int | None = None) -> bytes:
    """Shareable wallet PnL card (same look as the call PnL cards)."""
    rng = random.Random(seed)
    win = pnl >= 0
    t = WIN if win else LOSS
    W, H = 12, 6.75
    fig = plt.figure(figsize=(W, H), dpi=110)
    bg = fig.add_axes([0, 0, 1, 1])
    bg.axis("off")
    import numpy as np
    yy, xx = np.mgrid[0:1:200j, 0:1:356j]
    c1 = np.array(matplotlib.colors.to_rgb(t["bg1"]))
    c2 = np.array(matplotlib.colors.to_rgb(t["bg2"]))
    mix = np.clip((xx * 0.6 + (1 - yy) * 0.4), 0, 1)[..., None]
    img = c1 * (1 - mix) + c2 * mix
    blob = np.exp(-(((xx - 0.78) / 0.28) ** 2 + ((yy - 0.45) / 0.45) ** 2))[..., None]
    img = np.clip(img + blob * np.array(matplotlib.colors.to_rgb(t["glow"])) * 0.28, 0, 1)
    bg.imshow(img, extent=(0, 1, 0, 1), aspect="auto", origin="lower")
    bg.set_xlim(0, 1)
    bg.set_ylim(0, 1)
    for _ in range(70 if win else 45):
        x, y = rng.random(), rng.random()
        if win:
            bg.add_patch(P.Rectangle((x, y), 0.006, 0.012, angle=rng.random() * 90,
                                     color=rng.choice([t["main"], t["accent"], t["pink"], "#4cc9f0", "white"]),
                                     alpha=0.55))
        else:
            bg.plot([x, x - 0.01], [y, y - 0.05], color=t["pink"], lw=1, alpha=0.25)
    if points and len(points) >= 3:  # cumulative PnL over the period
        lo, hi = min(points), max(points)
        span = (hi - lo) or 1
        xs = [0.02 + 0.96 * i / (len(points) - 1) for i in range(len(points))]
        ys = [0.03 + 0.22 * (p - lo) / span for p in points]
        bg.plot(xs, ys, color=t["main"], lw=3, alpha=0.35)
        bg.fill_between(xs, ys, 0, color=t["main"], alpha=0.07)

    short = wallet if len(wallet) <= 26 else wallet[:12] + "…" + wallet[-11:]
    bg.text(0.045, 0.9, short, color="white", fontsize=30, fontweight="bold", va="center",
            path_effects=glow(8, t["glow"], 0.35))
    bg.text(0.047, 0.81, f"Wallet PnL  ·  {period_label}", color="#cfd6e4", fontsize=16, va="center")
    pill = rng.choice(WIN_TAGS if win else LOSS_TAGS)
    bg.text(0.047, 0.725, f"  {pill}  ", color="#0b0f2e" if win else "white", fontsize=15, fontweight="bold",
            va="center", bbox=dict(boxstyle="round,pad=0.35", fc=t["accent"] if win else t["main"], ec="none"))
    bg.text(0.042, 0.545, money(pnl, sign=True), color=t["main"], fontsize=70, fontweight="bold", va="center",
            path_effects=glow(18, t["glow"], 0.45))
    sub = f"{pct:+.0f}% ROI" + (f"  ·  best ${best[:14]}" if best else "")
    bg.text(0.047, 0.4, sub, color="white", fontsize=17, va="center", alpha=0.92)
    chips = [("BOUGHT", money(bought), "white"), ("SOLD", money(sold), t["main"]),
             ("HOLD", money(hold), t["accent"]), ("WIN RATE", f"{wins}/{tokens}", "white")]
    x = 0.045
    for label, val, col in chips:
        w = 0.05 + 0.0115 * max(len(val), len(label) * 0.7)
        bg.add_patch(P.FancyBboxPatch((x, 0.2), w + 0.012, 0.11, boxstyle="round,pad=0,rounding_size=0.025",
                                      fc=(1, 1, 1, 0.08), ec=(1, 1, 1, 0.18), lw=1.2))
        bg.text(x + 0.012, 0.283, label, color="#aab3c5", fontsize=10, fontweight="bold", va="center")
        bg.text(x + 0.012, 0.235, val, color=col, fontsize=15, fontweight="bold", va="center")
        x += w + 0.026
    art = fig.add_axes([0.58, 0.06, 0.4, 0.4 * W / H * 0.97])
    art.set_aspect("equal")
    draw_art(art, win, t, rng)
    bg.text(0.955, 0.93, "ELYSIUM ANALYZER", color="white", fontsize=14, fontweight="bold", ha="right", va="center",
            alpha=0.85)
    bg.text(0.045, 0.06, datetime.now(timezone.utc).strftime("%d %b %Y · %H:%M UTC") + "",
            color="white", fontsize=10, alpha=0.45, va="center")
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()
