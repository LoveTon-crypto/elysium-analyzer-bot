"""Settings from .env (see .env.example)."""
from __future__ import annotations

import os

from dotenv import load_dotenv

BASE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE, ".env"))


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def env_list(name: str, default: str = "") -> list[str]:
    return [x.strip() for x in env(name, default).split(",") if x.strip()]


BOT_TOKEN = env("BOT_TOKEN")
OWNER_ID = env("OWNER_ID")
DB_PATH = env("DB_PATH") or os.path.join(BASE, "analyzer.db")

# ---- chain (same nodes as the Degen trade bot)
CHAIN_ID = int(env("CHAIN_ID", "99901"))
RPC_URLS = env_list("RPC_URLS", "https://rpc.thekleos.xyz,https://rpc-live.boxa.club,https://rpc.thekleos.pro/rpc,"
                                 "https://rpc-live.elyspump.com,https://rpc-live.elysp.com")
EXPLORER = env("EXPLORER_URL", "https://elyscan.live").rstrip("/")
# Blockscout-style explorer API for top holders (empty = no holders on the scan card)
EXPLORER_API = env("EXPLORER_API", EXPLORER + "/api/v2").rstrip("/")
ROUTERS = env("ROUTERS")                       # empty = venues built into dex.py (same as the trade bot)
HYPE_USD_API = env("HYPE_USD_API", "https://api.hyperliquid.xyz/info")
IPFS_GATEWAY = env("IPFS_GATEWAY", "https://ipfs.io/ipfs/")

# ---- buy buttons
DEGEN_BOT = env("DEGEN_BOT", "elysiumdegen_bot").lstrip("@")
DEGEN_REF = env("DEGEN_REF")                   # your referral code in Degen bot (🤝 Referrals), optional
BASED_BOT = env("BASED_BOT", "based_eth_bot").lstrip("@")
BASED_REF = env("BASED_REF", "r_Ramram")
# Start parameter of Based bot for one token, {ref} and {token} are replaced. Empty = referral link only.
BASED_TOKEN_PARAM = env("BASED_TOKEN_PARAM", "{ref}_b_{token}")

# ---- deploys channel
DEPLOY_CHANNEL = env("DEPLOY_CHANNEL", "@elysium_deploys")
DEPLOY_POLL = float(env("DEPLOY_POLL", "1"))           # s between new-block checks
DEPLOY_ROUTE_WAIT = int(env("DEPLOY_ROUTE_WAIT", "90"))  # s to wait for the token's pool to show up
DEPLOY_REFRESH = int(env("DEPLOY_REFRESH", "25"))     # s: edit the post once (dev buy, MC); 0 = off
DEPLOY_MAX_LAG = int(env("DEPLOY_MAX_LAG", "3000"))   # blocks; further behind → jump to the head
POST_ALL = env("DEPLOY_POST_ALL", "0") == "1"          # also post tokens not launched on a memepad
BACKFILL_BLOCKS = int(env("BACKFILL_BLOCKS", "0"))      # once: learn older dev deploys from N past blocks
# Extra launch contracts: tx.to address = LABEL, comma-separated (see /pads)
PAD_FACTORIES = {}
for _kv in env_list("PAD_FACTORIES"):
    if "=" in _kv:
        _a, _l = _kv.split("=", 1)
        PAD_FACTORIES[_a.strip().lower()] = _l.strip().upper()
# Token page per launchpad label ({token} = address); unknown labels open the explorer
PAD_PAGES = {
    "KLEOS": "https://www.thekleos.xyz/token/{token}",
    "ELYSPUMP": "https://elysp.com/#/token/{token}",
    "MONTRA": "https://montramarkets.xyz/token/{token}",
    "STOCKIUM": "https://stockium.fun/token/{token}",
}
for _kv in env_list("PAD_PAGES"):
    if "=" in _kv:
        PAD_PAGES[_kv.split("=", 1)[0].strip().upper()] = _kv.split("=", 1)[1].strip()

# ---- tracking & group alerts
TRACK_SECONDS = int(env("TRACK_SECONDS", "60"))
TRACK_DAYS = float(env("TRACK_DAYS", "3"))             # memepad tokens younger than this are watched
TRACK_MAX = int(env("TRACK_MAX", "400"))
TRACK_MIN_MC = float(env("TRACK_MIN_MC", "3000"))      # older tokens stay watched while MC ≥ this ($)
ATH_DAYS = float(env("ATH_DAYS", "30"))
MCAP_LEVELS = sorted(int(float(x)) for x in env_list("MCAP_LEVELS", "10000,20000,30000,50000,100000"))
DIP_PCT = float(env("DIP_PCT", "15"))
DIP_WINDOW_MIN = int(env("DIP_WINDOW_MIN", "60"))
DIP_COOLDOWN_MIN = int(env("DIP_COOLDOWN_MIN", "60"))
DIP_MIN_LIQ = float(env("DIP_MIN_LIQ", "300"))          # $ of HYPE in the pool
PAD_EXTRA = [x.lower() for x in env_list("PAD_EXTRA")]
