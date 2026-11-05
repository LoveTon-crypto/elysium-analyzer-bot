"""Original degen characters for PnL cards, drawn in a sticker style (thick outlines, shading, highlights).

Win:  Chad Bull, AI Agent on a rocket, Astro Cat on the moon, King Whale, Raccoon making it rain
Loss: Grinning Bear, Broken AI Agent, Cat in a box, Raccoon back at the burger stand, Sad Clown
Each function draws on a 0..1 square axes; `t` is the card theme (colors).
"""
from __future__ import annotations

import math
import random

from matplotlib import patches as P
from matplotlib import patheffects as pe
from matplotlib.path import Path

OUT = "#16121f"   # outline color
LW = 3.2


def _fx(lw=LW):
    return dict(ec=OUT, lw=lw, joinstyle="round")


def ell(ax, x, y, w, h, c, z=2, angle=0, lw=LW, alpha=1.0):
    ax.add_patch(P.Ellipse((x, y), w, h, angle=angle, fc=c, zorder=z, alpha=alpha, **_fx(lw)))


def blob(ax, x, y, w, h, c, z=2, angle=0):
    ax.add_patch(P.Ellipse((x, y), w, h, angle=angle, fc=c, ec="none", zorder=z))


def poly(ax, pts, c, z=2, lw=LW, closed=True):
    ax.add_patch(P.Polygon(pts, closed=closed, fc=c, zorder=z, **_fx(lw)))


def rrect(ax, x, y, w, h, r, c, z=2, lw=LW):
    ax.add_patch(P.FancyBboxPatch((x, y), w, h, boxstyle=f"round,pad=0,rounding_size={r}", fc=c, zorder=z, **_fx(lw)))


def curve(ax, pts, c="none", z=2, lw=LW, fill=True, ec=OUT):
    """Smooth closed shape through bezier control points: pts = [start, c1, c2, end, c1, c2, end, ...]."""
    codes = [Path.MOVETO] + [Path.CURVE4] * (len(pts) - 1)
    if fill:
        pts = pts + [pts[0]]
        codes = codes + [Path.CLOSEPOLY]
    ax.add_patch(P.PathPatch(Path(pts, codes), fc=c if fill else "none", ec=ec, lw=lw, zorder=z, joinstyle="round",
                             capstyle="round"))


def line(ax, xs, ys, c=OUT, lw=LW, z=3):
    ax.plot(xs, ys, color=c, lw=lw, zorder=z, solid_capstyle="round", solid_joinstyle="round")


def arc(ax, x, y, w, h, t1, t2, c=OUT, lw=LW, z=4):
    ax.add_patch(P.Arc((x, y), w, h, theta1=t1, theta2=t2, color=c, lw=lw, zorder=z, capstyle="round"))


def eye(ax, x, y, r, look=(0.0, 0.0), z=5, pupil=0.55):
    ell(ax, x, y, r * 2, r * 2.2, "white", z=z, lw=2.4)
    blob(ax, x + look[0] * r * 0.4, y + look[1] * r * 0.4, r * 2 * pupil, r * 2.2 * pupil, OUT, z=z + 1)
    blob(ax, x + look[0] * r * 0.4 - r * 0.25, y + look[1] * r * 0.4 + r * 0.3, r * 0.45, r * 0.45, "white", z=z + 2)


def shades(ax, x, y, w, z=8):
    h = w * 0.28
    for dx in (-w * 0.27, w * 0.27):
        poly(ax, [(x + dx - w * 0.24, y + h / 2), (x + dx + w * 0.24, y + h / 2), (x + dx + w * 0.2, y - h / 2),
                  (x + dx - w * 0.17, y - h / 2)], "#0d0d14", z=z, lw=2.5)
        poly(ax, [(x + dx - w * 0.18, y + h * 0.35), (x + dx - w * 0.06, y + h * 0.35), (x + dx - w * 0.14, y - h * 0.1)],
             "white", z=z + 1, lw=0)
    line(ax, [x - w * 0.05, x + w * 0.05], [y + h * 0.35, y + h * 0.35], OUT, 4, z)
    line(ax, [x - w * 0.5, x - w * 0.62], [y + h * 0.4, y + h * 0.55], OUT, 3, z)
    line(ax, [x + w * 0.5, x + w * 0.62], [y + h * 0.4, y + h * 0.55], OUT, 3, z)


def star(ax, x, y, s, c, z=1, lw=2):
    pts = []
    for i in range(10):
        a = math.pi / 2 + i * math.pi / 5
        r = s if i % 2 == 0 else s * 0.45
        pts.append((x + r * math.cos(a), y + r * math.sin(a)))
    poly(ax, pts, c, z=z, lw=lw)


def coin(ax, x, y, r, z=6, label="H"):
    ell(ax, x, y, r * 2, r * 2, "#ffd23f", z=z, lw=2.4)
    ell(ax, x, y, r * 1.45, r * 1.45, "#ffc300", z=z + 1, lw=1.5)
    ax.text(x, y, label, color="#7a5200", fontsize=r * 160, fontweight="bold", ha="center", va="center",
            zorder=z + 2)


def bill(ax, x, y, ang, z=6):
    ax.add_patch(P.FancyBboxPatch((x - 0.06, y - 0.03), 0.12, 0.06, boxstyle="round,pad=0,rounding_size=0.008",
                                  fc="#52b788", ec="#1b4332", lw=2, zorder=z,
                                  transform=_rot(ax, x, y, ang)))
    ax.text(x, y, "$", color="#1b4332", fontsize=11, fontweight="bold", ha="center", va="center", zorder=z + 1,
            rotation=ang)


def _rot(ax, x, y, ang):
    import matplotlib.transforms as T
    return T.Affine2D().rotate_deg_around(x, y, ang) + ax.transData


def sticker_shadow(ax, x, y, w, h):
    blob(ax, x, y, w, h, (0, 0, 0, 0.28), z=0)


# ======================= WIN =======================
def chad_bull(ax, t):
    sticker_shadow(ax, 0.5, 0.06, 0.6, 0.06)
    # body + chain
    curve(ax, [(0.2, 0.05), (0.18, 0.3), (0.3, 0.36), (0.5, 0.36), (0.7, 0.36), (0.82, 0.3), (0.8, 0.05)], "#7b4a26", z=1)
    arc(ax, 0.5, 0.33, 0.36, 0.26, 200, 340, "#ffd23f", 7, z=3)
    ell(ax, 0.5, 0.2, 0.11, 0.11, "#ffd23f", z=4, lw=2.4)
    ax.text(0.5, 0.2, "$", color="#7a5200", fontsize=18, fontweight="bold", ha="center", va="center", zorder=5)
    # horns
    curve(ax, [(0.27, 0.62), (0.15, 0.66), (0.06, 0.78), (0.1, 0.92), (0.14, 0.8), (0.22, 0.73), (0.32, 0.7)], "#f3e3c3", z=2)
    curve(ax, [(0.73, 0.62), (0.85, 0.66), (0.94, 0.78), (0.9, 0.92), (0.86, 0.8), (0.78, 0.73), (0.68, 0.7)], "#f3e3c3", z=2)
    # ears
    ell(ax, 0.22, 0.6, 0.16, 0.08, "#8b5a2b", z=2, angle=20)
    ell(ax, 0.78, 0.6, 0.16, 0.08, "#8b5a2b", z=2, angle=-20)
    # head
    curve(ax, [(0.5, 0.82), (0.7, 0.82), (0.75, 0.62), (0.68, 0.45), (0.62, 0.34), (0.38, 0.34), (0.32, 0.45),
               (0.25, 0.62), (0.3, 0.82), (0.5, 0.82)], "#8b5a2b", z=3)
    blob(ax, 0.42, 0.72, 0.12, 0.06, (1, 1, 1, 0.18), z=4)
    # snout + ring
    ell(ax, 0.5, 0.43, 0.32, 0.18, "#f0a986", z=5)
    ell(ax, 0.44, 0.43, 0.045, 0.06, "#7a3b2a", z=6, lw=1.5)
    ell(ax, 0.56, 0.43, 0.045, 0.06, "#7a3b2a", z=6, lw=1.5)
    arc(ax, 0.5, 0.36, 0.1, 0.08, 200, 340, "#c9a227", 5, z=7)
    # smirk
    arc(ax, 0.55, 0.38, 0.14, 0.06, 200, 320, OUT, 3, z=7)
    shades(ax, 0.5, 0.6, 0.36)
    star(ax, 0.88, 0.45, 0.05, t["accent"])
    star(ax, 0.1, 0.4, 0.035, t["accent"])


def ai_rocket(ax, t):
    # flame + rocket board
    for w, h, c in ((0.22, 0.32, "#ff6b35"), (0.14, 0.22, "#ffd23f"), (0.07, 0.12, "#fff6d5")):
        curve(ax, [(0.5 - w / 2, 0.28), (0.5 - w / 2, 0.2), (0.5, 0.28 - h), (0.5, 0.28 - h), (0.5, 0.28 - h),
                   (0.5 + w / 2, 0.2), (0.5 + w / 2, 0.28)], c, z=1, lw=0 if c != "#ff6b35" else 2.5)
    rrect(ax, 0.22, 0.26, 0.56, 0.08, 0.04, "#e5e9f0", z=2)
    poly(ax, [(0.22, 0.3), (0.12, 0.24), (0.2, 0.34)], t["main"], z=2)
    poly(ax, [(0.78, 0.3), (0.88, 0.24), (0.8, 0.34)], t["main"], z=2)
    # body
    rrect(ax, 0.33, 0.33, 0.34, 0.2, 0.06, "#9aa9bb", z=3)
    ell(ax, 0.5, 0.43, 0.12, 0.12, "#2b2d42", z=4, lw=2)
    ax.text(0.5, 0.43, "N", color=t["main"], fontsize=17, fontweight="bold", ha="center", va="center", zorder=5)
    # arms: thumbs up
    line(ax, [0.67, 0.8], [0.47, 0.6], OUT, 9, 2)
    line(ax, [0.67, 0.8], [0.47, 0.6], "#9aa9bb", 6, 2)
    rrect(ax, 0.77, 0.57, 0.08, 0.08, 0.025, "#9aa9bb", z=4)
    rrect(ax, 0.79, 0.63, 0.035, 0.08, 0.017, "#9aa9bb", z=4)
    line(ax, [0.33, 0.2], [0.47, 0.42], OUT, 9, 2)
    line(ax, [0.33, 0.2], [0.47, 0.42], "#9aa9bb", 6, 2)
    ell(ax, 0.19, 0.42, 0.06, 0.06, "#9aa9bb", z=4)
    # head with screen face
    rrect(ax, 0.28, 0.52, 0.44, 0.3, 0.08, "#c5d0de", z=5)
    rrect(ax, 0.32, 0.56, 0.36, 0.22, 0.05, "#0b1a2a", z=6, lw=2)
    for x in (0.42, 0.58):
        arc(ax, x, 0.66, 0.07, 0.07, 20, 160, t["main"], 5, z=7)
    arc(ax, 0.5, 0.62, 0.14, 0.08, 200, 340, t["main"], 5, z=7)
    blob(ax, 0.37, 0.74, 0.05, 0.025, (1, 1, 1, 0.25), z=7)
    line(ax, [0.5, 0.5], [0.82, 0.9], OUT, 3, 4)
    ell(ax, 0.5, 0.92, 0.05, 0.05, t["accent"], z=5, lw=2)
    for a in range(0, 360, 45):
        line(ax, [0.5 + 0.035 * math.cos(math.radians(a)), 0.5 + 0.055 * math.cos(math.radians(a))],
             [0.92 + 0.035 * math.sin(math.radians(a)), 0.92 + 0.055 * math.sin(math.radians(a))], t["accent"], 2, 5)
    for x, y, s in ((0.1, 0.8, 0.04), (0.88, 0.85, 0.03), (0.9, 0.2, 0.025)):
        star(ax, x, y, s, "white")


def astro_cat(ax, t):
    # moon ground
    curve(ax, [(-0.05, 0.0), (-0.05, 0.2), (0.3, 0.26), (0.55, 0.24), (0.8, 0.22), (1.05, 0.18), (1.05, 0.0)], "#d9d4c7", z=1)
    for x, y, w in ((0.15, 0.12, 0.12), (0.7, 0.08, 0.16), (0.88, 0.15, 0.08)):
        ell(ax, x, y, w, w * 0.4, "#bfb8a6", z=2, lw=2)
    # flag
    line(ax, [0.8, 0.8], [0.22, 0.75], OUT, 4, 2)
    poly(ax, [(0.8, 0.75), (0.97, 0.7), (0.8, 0.63)], t["main"], z=3)
    ax.text(0.855, 0.69, "N", color=OUT, fontsize=13, fontweight="bold", ha="center", va="center", zorder=4)
    # suit body
    rrect(ax, 0.3, 0.2, 0.36, 0.3, 0.1, "#f1f3f8", z=3)
    rrect(ax, 0.4, 0.28, 0.16, 0.1, 0.02, "#adb5bd", z=4, lw=2)
    for i, c in enumerate(("#ff4d6d", t["main"], "#4cc9f0")):
        ell(ax, 0.44 + i * 0.04, 0.33, 0.025, 0.025, c, z=5, lw=1.2)
    ell(ax, 0.26, 0.38, 0.1, 0.18, "#f1f3f8", z=2, angle=-30)
    # arm holding flag
    ell(ax, 0.71, 0.45, 0.09, 0.16, "#f1f3f8", z=2, angle=40)
    ell(ax, 0.78, 0.5, 0.07, 0.07, "#f1f3f8", z=4)
    # helmet + cat face
    ell(ax, 0.48, 0.64, 0.42, 0.4, "#bde0fe", z=5)
    poly(ax, [(0.36, 0.7), (0.34, 0.82), (0.43, 0.75)], "#f4a259", z=6, lw=2.4)
    poly(ax, [(0.6, 0.7), (0.62, 0.82), (0.53, 0.75)], "#f4a259", z=6, lw=2.4)
    ell(ax, 0.48, 0.63, 0.3, 0.26, "#f4a259", z=6, lw=2.4)
    for dx in (-0.04, 0.0, 0.04):
        line(ax, [0.48 + dx, 0.48 + dx * 1.2], [0.75, 0.71], "#c96f1c", 2.5, 7)
    ell(ax, 0.48, 0.57, 0.14, 0.08, "#fde4c8", z=7, lw=0)
    eye(ax, 0.42, 0.65, 0.035, (0.5, 0.5), z=8)
    eye(ax, 0.54, 0.65, 0.035, (0.5, 0.5), z=8)
    poly(ax, [(0.47, 0.595), (0.49, 0.595), (0.48, 0.58)], "#ff8fab", z=9, lw=1.5)
    arc(ax, 0.465, 0.57, 0.03, 0.025, 200, 360, OUT, 2, 9)
    arc(ax, 0.495, 0.57, 0.03, 0.025, 180, 340, OUT, 2, 9)
    for s in (-1, 1):
        line(ax, [0.48 + s * 0.06, 0.48 + s * 0.15], [0.58, 0.6], OUT, 1.5, 9)
        line(ax, [0.48 + s * 0.06, 0.48 + s * 0.15], [0.57, 0.55], OUT, 1.5, 9)
    blob(ax, 0.38, 0.76, 0.1, 0.05, (1, 1, 1, 0.55), z=10, angle=25)
    ax.add_patch(P.Ellipse((0.48, 0.64), 0.42, 0.4, fc="none", zorder=10, **_fx()))
    for x, y, s in ((0.1, 0.85, 0.035), (0.25, 0.95, 0.02), (0.95, 0.9, 0.025)):
        star(ax, x, y, s, "white")


def king_whale(ax, t):
    # waves
    for i in range(6):
        arc(ax, i * 0.2, 0.1, 0.2, 0.1, 0, 180, "#4cc9f0", 4, z=1)
    # tail
    curve(ax, [(0.12, 0.4), (0.04, 0.5), (0.0, 0.62), (0.05, 0.7), (0.1, 0.6), (0.15, 0.55), (0.2, 0.5)], "#3a86ff", z=2)
    curve(ax, [(0.12, 0.4), (0.06, 0.32), (0.0, 0.25), (0.06, 0.2), (0.12, 0.28), (0.16, 0.34), (0.22, 0.4)], "#3a86ff", z=2)
    # body
    curve(ax, [(0.15, 0.42), (0.2, 0.7), (0.55, 0.74), (0.75, 0.66), (0.95, 0.58), (0.97, 0.3), (0.8, 0.22),
               (0.6, 0.12), (0.3, 0.18), (0.15, 0.42)], "#3a86ff", z=3)
    curve(ax, [(0.35, 0.2), (0.55, 0.12), (0.8, 0.2), (0.95, 0.35), (0.85, 0.3), (0.6, 0.24), (0.35, 0.2)],
          "#cfe8ff", z=4, lw=2.4)
    for x in (0.5, 0.6, 0.7, 0.8):
        line(ax, [x - 0.02, x + 0.02], [0.22 - (x - 0.5) * 0.05, 0.27 - (x - 0.5) * 0.02], "#9ac7f5", 2, 5)
    blob(ax, 0.45, 0.62, 0.22, 0.06, (1, 1, 1, 0.2), z=4)
    eye(ax, 0.75, 0.47, 0.04, (0.4, 0.3), z=6)
    arc(ax, 0.84, 0.36, 0.14, 0.08, 200, 320, OUT, 3.5, 6)
    ell(ax, 0.68, 0.38, 0.06, 0.035, "#ff8fab", z=6, lw=0)
    # crown
    poly(ax, [(0.6, 0.7), (0.6, 0.83), (0.65, 0.76), (0.7, 0.86), (0.75, 0.76), (0.8, 0.83), (0.8, 0.68)], "#ffd23f", z=5)
    for x in (0.6, 0.7, 0.8):
        ell(ax, x, 0.84 if x != 0.7 else 0.87, 0.03, 0.03, "#ff4d6d", z=6, lw=1.5)
    # coin spout
    line(ax, [0.55, 0.5], [0.73, 0.82], "#bde0fe", 6, 2)
    for x, y, r in ((0.45, 0.88, 0.04), (0.55, 0.93, 0.035), (0.36, 0.8, 0.035), (0.3, 0.92, 0.03)):
        coin(ax, x, y, r)


def raccoon_rain(ax, t):
    for x, y, a in ((0.1, 0.85, 20), (0.25, 0.95, -15), (0.85, 0.9, 30), (0.92, 0.6, -25), (0.08, 0.55, 10),
                    (0.75, 0.75, 45), (0.2, 0.7, -35)):
        bill(ax, x, y, a, z=1)
    # body in suit
    curve(ax, [(0.25, 0.02), (0.24, 0.28), (0.35, 0.36), (0.5, 0.36), (0.65, 0.36), (0.76, 0.28), (0.75, 0.02)], "#2b2d42", z=2)
    poly(ax, [(0.44, 0.36), (0.5, 0.18), (0.56, 0.36)], "white", z=3, lw=2)
    poly(ax, [(0.48, 0.33), (0.5, 0.2), (0.52, 0.33)], "#e63946", z=4, lw=1.5)
    # arms with money fans
    line(ax, [0.28, 0.12], [0.26, 0.42], OUT, 12, 2)
    line(ax, [0.28, 0.12], [0.26, 0.42], "#2b2d42", 8, 2)
    line(ax, [0.72, 0.88], [0.26, 0.42], OUT, 12, 2)
    line(ax, [0.72, 0.88], [0.26, 0.42], "#2b2d42", 8, 2)
    for cx, side in ((0.12, -1), (0.88, 1)):
        for k in range(4):
            bill(ax, cx + side * 0.02 * k, 0.47 + 0.01 * k, side * (-20 + 15 * k), z=5)
        ell(ax, cx, 0.42, 0.07, 0.07, "#8d99ae", z=6)
    # ears
    for s in (-1, 1):
        poly(ax, [(0.5 + s * 0.16, 0.72), (0.5 + s * 0.24, 0.9), (0.5 + s * 0.06, 0.8)], "#8d99ae", z=3)
        poly(ax, [(0.5 + s * 0.17, 0.76), (0.5 + s * 0.21, 0.85), (0.5 + s * 0.1, 0.8)], "#2b2d42", z=4, lw=0)
    # head
    curve(ax, [(0.5, 0.84), (0.72, 0.84), (0.78, 0.62), (0.66, 0.44), (0.58, 0.36), (0.42, 0.36), (0.34, 0.44),
               (0.22, 0.62), (0.28, 0.84), (0.5, 0.84)], "#adb5bd", z=4)
    curve(ax, [(0.5, 0.56), (0.62, 0.56), (0.66, 0.44), (0.58, 0.38), (0.54, 0.36), (0.46, 0.36), (0.42, 0.38),
               (0.34, 0.44), (0.38, 0.56), (0.5, 0.56)], "#f1f3f8", z=5, lw=0)
    curve(ax, [(0.27, 0.66), (0.35, 0.74), (0.45, 0.7), (0.5, 0.64), (0.55, 0.7), (0.65, 0.74), (0.73, 0.66),
               (0.66, 0.56), (0.56, 0.58), (0.5, 0.6), (0.44, 0.58), (0.34, 0.56), (0.27, 0.66)], "#2b2d42", z=5, lw=0)
    eye(ax, 0.41, 0.645, 0.035, (0.3, 0.2), z=6)
    eye(ax, 0.59, 0.645, 0.035, (-0.3, 0.2), z=6)
    ell(ax, 0.5, 0.5, 0.07, 0.045, OUT, z=6, lw=0)
    arc(ax, 0.5, 0.45, 0.16, 0.08, 200, 340, OUT, 3.5, 6)
    # top hat
    rrect(ax, 0.38, 0.84, 0.24, 0.03, 0.01, "#111", z=6, lw=2.5)
    rrect(ax, 0.42, 0.86, 0.16, 0.12, 0.015, "#111", z=6, lw=2.5)
    ax.add_patch(P.Rectangle((0.42, 0.875), 0.16, 0.025, fc=t["main"], ec="none", zorder=7))


# ======================= LOSS =======================
def grin_bear(ax, t):
    # red candles behind
    rng = random.Random(4)
    for i, x in enumerate((0.08, 0.2, 0.82, 0.93)):
        h = 0.18 + rng.random() * 0.15
        line(ax, [x, x], [0.65 - i * 0.03, 0.95 - i * 0.03], "#ff4d6d", 2, 0)
        rrect(ax, x - 0.03, 0.7 - i * 0.03, 0.06, h, 0.005, "#ff4d6d", z=1, lw=2)
    # body + claws
    curve(ax, [(0.18, 0.0), (0.15, 0.3), (0.3, 0.38), (0.5, 0.38), (0.7, 0.38), (0.85, 0.3), (0.82, 0.0)], "#5c3a21", z=2)
    ell(ax, 0.5, 0.16, 0.34, 0.3, "#8a5a36", z=3, lw=0)
    for cx in (0.2, 0.8):
        ell(ax, cx, 0.24, 0.16, 0.12, "#5c3a21", z=4)
        for k in (-1, 0, 1):
            poly(ax, [(cx + k * 0.04 - 0.012, 0.29), (cx + k * 0.04, 0.33), (cx + k * 0.04 + 0.012, 0.29)], "white",
                 z=5, lw=1.5)
    # ears
    for s in (-1, 1):
        ell(ax, 0.5 + s * 0.22, 0.82, 0.14, 0.14, "#5c3a21", z=3)
        ell(ax, 0.5 + s * 0.22, 0.82, 0.07, 0.07, "#c98c63", z=4, lw=0)
    # head
    ell(ax, 0.5, 0.62, 0.56, 0.48, "#6f4528", z=4)
    blob(ax, 0.4, 0.78, 0.16, 0.06, (1, 1, 1, 0.12), z=5)
    ell(ax, 0.5, 0.52, 0.3, 0.22, "#c98c63", z=5)
    ell(ax, 0.5, 0.58, 0.1, 0.06, OUT, z=6, lw=0)
    # evil brows + eyes
    eye(ax, 0.39, 0.68, 0.035, (0.4, -0.2), z=6)
    eye(ax, 0.61, 0.68, 0.035, (-0.4, -0.2), z=6)
    line(ax, [0.32, 0.45], [0.76, 0.72], OUT, 6, 8)
    line(ax, [0.68, 0.55], [0.76, 0.72], OUT, 6, 8)
    # toothy grin
    curve(ax, [(0.36, 0.5), (0.4, 0.4), (0.6, 0.4), (0.64, 0.5), (0.56, 0.47), (0.44, 0.47), (0.36, 0.5)], "#3d0d14", z=6, lw=2.5)
    for x in (0.42, 0.47, 0.53, 0.58):
        poly(ax, [(x - 0.02, 0.485), (x + 0.02, 0.485), (x, 0.455)], "white", z=7, lw=1.2)


def broken_bot(ax, t):
    # smoke
    for x, y, r in ((0.6, 0.9, 0.06), (0.68, 0.95, 0.05), (0.54, 0.97, 0.04)):
        ell(ax, x, y, r * 2, r * 1.6, "#6c757d", z=1, lw=2, alpha=0.9)
    # body slumped
    rrect(ax, 0.3, 0.1, 0.4, 0.24, 0.06, "#7d8a99", z=2)
    ell(ax, 0.5, 0.22, 0.12, 0.12, "#2b2d42", z=3, lw=2)
    ax.text(0.5, 0.22, "!", color="#ff4d6d", fontsize=20, fontweight="bold", ha="center", va="center", zorder=4)
    line(ax, [0.3, 0.16, 0.12], [0.26, 0.18, 0.06], OUT, 9, 1)
    line(ax, [0.3, 0.16, 0.12], [0.26, 0.18, 0.06], "#7d8a99", 6, 1)
    line(ax, [0.7, 0.84, 0.88], [0.26, 0.18, 0.06], OUT, 9, 1)
    line(ax, [0.7, 0.84, 0.88], [0.26, 0.18, 0.06], "#7d8a99", 6, 1)
    # head tilted
    import matplotlib.transforms as T
    tr = T.Affine2D().rotate_deg_around(0.5, 0.52, -12) + ax.transData
    ax.add_patch(P.FancyBboxPatch((0.26, 0.34), 0.48, 0.34, boxstyle="round,pad=0,rounding_size=0.08", fc="#aab4c3",
                                  zorder=4, transform=tr, **_fx()))
    ax.add_patch(P.FancyBboxPatch((0.3, 0.38), 0.4, 0.26, boxstyle="round,pad=0,rounding_size=0.05", fc="#0b1a2a",
                                  zorder=5, transform=tr, **_fx(2)))
    for x in (0.4, 0.6):
        for d in (-1, 1):
            ax.plot([x - 0.035, x + 0.035], [0.54 - d * 0.035, 0.54 + d * 0.035], color=t["main"], lw=5, zorder=6,
                    transform=tr, solid_capstyle="round")
    ax.add_patch(P.Arc((0.5, 0.44), 0.16, 0.08, theta1=20, theta2=160, color=t["main"], lw=5, zorder=6, transform=tr))
    # crack
    ax.plot([0.55, 0.6, 0.57, 0.65, 0.63, 0.7], [0.64, 0.57, 0.52, 0.47, 0.42, 0.38], color="white", lw=2.5, zorder=7,
            transform=tr)
    # antenna bent + sparks
    line(ax, [0.47, 0.45, 0.36], [0.68, 0.76, 0.8], OUT, 3, 3)
    for a in range(0, 360, 40):
        r1, r2 = 0.02, 0.06 + (a % 80) / 2000
        line(ax, [0.35 + r1 * math.cos(math.radians(a)), 0.35 + r2 * math.cos(math.radians(a))],
             [0.81 + r1 * math.sin(math.radians(a)), 0.81 + r2 * math.sin(math.radians(a))], t["accent"], 2.5, 5)
    ax.text(0.5, 0.02, "ERR: LIQUIDATED", color=t["main"], fontsize=12, fontweight="bold", ha="center",
            va="bottom", zorder=8, family="monospace")


def box_cat(ax, t):
    # rain cloud
    for x, y, w in ((0.35, 0.9, 0.2), (0.5, 0.93, 0.24), (0.64, 0.9, 0.2)):
        ell(ax, x, y, w, w * 0.6, "#6c757d", z=1, lw=2.4)
    blob(ax, 0.5, 0.88, 0.4, 0.08, "#6c757d", z=2)
    for x in (0.36, 0.45, 0.54, 0.63):
        line(ax, [x, x - 0.02], [0.82, 0.76], "#4cc9f0", 2.5, 1)
    # cat head peeking
    for s in (-1, 1):
        poly(ax, [(0.5 + s * 0.08, 0.6), (0.5 + s * 0.17, 0.74), (0.5 + s * 0.18, 0.54)], "#495057", z=2)
        poly(ax, [(0.5 + s * 0.11, 0.6), (0.5 + s * 0.16, 0.69), (0.5 + s * 0.16, 0.57)], "#ff8fab", z=3, lw=0)
    ell(ax, 0.5, 0.5, 0.36, 0.28, "#495057", z=3)
    eye(ax, 0.43, 0.52, 0.05, (0, -0.3), z=5, pupil=0.75)
    eye(ax, 0.57, 0.52, 0.05, (0, -0.3), z=5, pupil=0.75)
    blob(ax, 0.42, 0.465, 0.02, 0.04, "#4cc9f0", z=8)
    poly(ax, [(0.49, 0.47), (0.51, 0.47), (0.5, 0.455)], "#ff8fab", z=6, lw=1.2)
    arc(ax, 0.5, 0.43, 0.06, 0.03, 20, 160, OUT, 2, 6)
    # box
    poly(ax, [(0.18, 0.42), (0.82, 0.42), (0.82, 0.04), (0.18, 0.04)], "#c08552", z=6)
    poly(ax, [(0.18, 0.42), (0.08, 0.3), (0.2, 0.26), (0.28, 0.42)], "#a0673a", z=7)
    poly(ax, [(0.82, 0.42), (0.92, 0.3), (0.8, 0.26), (0.72, 0.42)], "#a0673a", z=7)
    line(ax, [0.5, 0.5], [0.42, 0.3], "#8b5a2b", 3, 7)
    # sign
    rrect(ax, 0.26, 0.1, 0.48, 0.16, 0.01, "#f8f1e5", z=8, lw=2.4)
    ax.text(0.5, 0.18, "WILL TRADE\nFOR FOOD", color=OUT, fontsize=12, fontweight="bold", ha="center", va="center",
            zorder=9, linespacing=1.1)


def burger_raccoon(ax, t):
    # grill
    rrect(ax, 0.05, 0.02, 0.9, 0.16, 0.02, "#495057", z=1)
    for x in (0.15, 0.3, 0.45, 0.6, 0.75):
        line(ax, [x, x + 0.08], [0.18, 0.18], "#adb5bd", 3, 2)
    # flying patty
    ell(ax, 0.8, 0.56, 0.2, 0.1, "#7f4f24", z=7, angle=15)
    for dx in (-0.05, 0.0, 0.05):
        line(ax, [0.8 + dx - 0.02, 0.8 + dx + 0.02], [0.54 + dx * 0.27, 0.58 + dx * 0.27], "#3d2410", 2.5, 8)
    for a in (0.72, 0.8, 0.88):
        arc(ax, a, 0.68, 0.03, 0.06, 90, 270, "#dee2e6", 2, 6)
    # spatula
    line(ax, [0.62, 0.74], [0.32, 0.44], "#6c757d", 5, 7)
    rrect(ax, 0.71, 0.42, 0.08, 0.05, 0.01, "#ced4da", z=7, lw=2)
    # body with apron
    curve(ax, [(0.22, 0.18), (0.2, 0.4), (0.32, 0.46), (0.5, 0.46), (0.68, 0.46), (0.8, 0.4), (0.78, 0.18)], "#8d99ae", z=3)
    rrect(ax, 0.36, 0.18, 0.28, 0.26, 0.03, "#f8f9fa", z=4, lw=2.4)
    ax.text(0.5, 0.3, "$7.25/h", color="#e63946", fontsize=11, fontweight="bold", ha="center", va="center", zorder=5)
    line(ax, [0.66, 0.64], [0.4, 0.34], OUT, 10, 5)
    line(ax, [0.66, 0.64], [0.4, 0.34], "#8d99ae", 7, 5)
    # head (same raccoon, sad)
    for s in (-1, 1):
        poly(ax, [(0.5 + s * 0.14, 0.78), (0.5 + s * 0.22, 0.92), (0.5 + s * 0.05, 0.84)], "#8d99ae", z=4)
    curve(ax, [(0.5, 0.88), (0.7, 0.88), (0.76, 0.7), (0.66, 0.54), (0.58, 0.46), (0.42, 0.46), (0.34, 0.54),
               (0.24, 0.7), (0.3, 0.88), (0.5, 0.88)], "#adb5bd", z=5)
    curve(ax, [(0.28, 0.72), (0.36, 0.8), (0.45, 0.76), (0.5, 0.7), (0.55, 0.76), (0.64, 0.8), (0.72, 0.72),
               (0.66, 0.62), (0.56, 0.64), (0.5, 0.66), (0.44, 0.64), (0.34, 0.62), (0.28, 0.72)], "#2b2d42", z=6, lw=0)
    eye(ax, 0.42, 0.7, 0.03, (0, -0.5), z=7)
    eye(ax, 0.58, 0.7, 0.03, (0, -0.5), z=7)
    line(ax, [0.36, 0.46], [0.76, 0.8], OUT, 3.5, 9)
    line(ax, [0.64, 0.54], [0.76, 0.8], OUT, 3.5, 9)
    ell(ax, 0.5, 0.58, 0.06, 0.04, OUT, z=7, lw=0)
    arc(ax, 0.5, 0.5, 0.12, 0.06, 30, 150, OUT, 3, 7)
    # paper cap
    poly(ax, [(0.32, 0.84), (0.68, 0.84), (0.62, 0.95), (0.38, 0.95)], "white", z=8, lw=2.4)
    ax.add_patch(P.Rectangle((0.32, 0.84), 0.36, 0.03, fc="#e63946", ec="none", zorder=9))


def sad_clown(ax, t):
    # hair puffs
    for s in (-1, 1):
        for dy in (0, 0.1):
            ell(ax, 0.5 + s * 0.3, 0.62 + dy, 0.18, 0.16, "#ff4fd8" if s < 0 else "#4cc9f0", z=1)
    # ruffle collar
    for i in range(7):
        ell(ax, 0.2 + i * 0.1, 0.17, 0.14, 0.12, "#ffd23f" if i % 2 else "#ff4d6d", z=2)
    # face
    ell(ax, 0.5, 0.55, 0.5, 0.56, "#fdfdfd", z=3)
    # eyes w/ makeup
    for x in (0.4, 0.6):
        poly(ax, [(x, 0.72), (x + 0.04, 0.62), (x, 0.55), (x - 0.04, 0.62)], "#3a86ff", z=4, lw=1.5)
        eye(ax, x, 0.64, 0.03, (0, -0.4), z=5)
    line(ax, [0.34, 0.45], [0.73, 0.75], OUT, 3, 7)
    line(ax, [0.66, 0.55], [0.73, 0.75], OUT, 3, 7)
    # tear streak
    curve(ax, [(0.4, 0.55), (0.37, 0.5), (0.36, 0.45), (0.38, 0.42), (0.41, 0.45), (0.41, 0.5), (0.4, 0.55)],
          "#4cc9f0", z=6, lw=1.5)
    # nose
    ell(ax, 0.5, 0.52, 0.1, 0.1, "#e63946", z=6)
    blob(ax, 0.48, 0.54, 0.025, 0.025, (1, 1, 1, 0.7), z=7)
    # big sad mouth
    curve(ax, [(0.36, 0.36), (0.42, 0.44), (0.58, 0.44), (0.64, 0.36), (0.58, 0.4), (0.42, 0.4), (0.36, 0.36)],
          "#e63946", z=6, lw=2.5)
    # tiny hat
    poly(ax, [(0.43, 0.82), (0.57, 0.82), (0.5, 0.97)], t["main"], z=4)
    ell(ax, 0.5, 0.97, 0.05, 0.05, "#ffd23f", z=5, lw=2)
    ax.text(0.85, 0.92, "honk", color="white", fontsize=14, fontweight="bold", ha="center", rotation=15,
            zorder=8, style="italic")


WIN_CHARS = [chad_bull, ai_rocket, astro_cat, king_whale, raccoon_rain]
LOSS_CHARS = [grin_bear, broken_bot, box_cat, burger_raccoon, sad_clown]

