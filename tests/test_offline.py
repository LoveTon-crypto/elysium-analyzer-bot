"""Offline checks with a fake chain: run `python3 tests/test_offline.py` (no network, no Telegram)."""
import asyncio
import os
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# ---- stubs for libs that may be missing where tests run
try:
    import telegram  # noqa: F401
except ImportError:
    tg = types.ModuleType("telegram")

    class _Btn:
        def __init__(self, text, url=None, callback_data=None):
            self.text, self.url, self.callback_data = text, url, callback_data

    class _KB:
        def __init__(self, rows):
            self.inline_keyboard = rows
    tg.InlineKeyboardButton, tg.InlineKeyboardMarkup = _Btn, _KB
    tg.LinkPreviewOptions = lambda **k: None
    const = types.ModuleType("telegram.constants")
    const.ParseMode = types.SimpleNamespace(HTML="HTML")
    sys.modules["telegram"], sys.modules["telegram.constants"] = tg, const
try:
    import httpx  # noqa: F401
except ImportError:
    hx = types.ModuleType("httpx")

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def get(self, *a, **k):
            raise RuntimeError("offline")

        async def post(self, *a, **k):
            raise RuntimeError("offline")
    hx.AsyncClient = _Client
    sys.modules["httpx"] = hx
try:
    import eth_utils  # noqa: F401
except ImportError:
    eu = types.ModuleType("eth_utils")
    eu.to_checksum_address = lambda a: a
    sys.modules["eth_utils"] = eu

os.environ["DB_PATH"] = tempfile.mktemp(suffix=".db")
os.environ["DEPLOY_ROUTE_WAIT"] = "0"
os.environ["DEPLOY_REFRESH"] = "0"

import links  # noqa: E402
import meta  # noqa: E402
from deploys import DeployFeed, is_lp  # noqa: E402
from dex import Route, Token, Venue  # noqa: E402
from market import Snap  # noqa: E402
from store import Store  # noqa: E402
from tracker import Tracker  # noqa: E402


def abi_str(s: str) -> str:
    b = s.encode()
    return f"{len(b):064x}" + b.hex().ljust((len(b) + 31) // 32 * 64, "0")


def test_meta():
    # launch(name, symbol, image, description, twitter, telegram) as a real ABI payload
    args = ["Moon Cat", "MCAT", "ipfs://QmYwAPJzv5CZsnA625s3Xf2nemtYgPpHdWEz79ojWnPbdG",
            "The first cat on Elysium, to the moon", "https://x.com/mooncat", "t.me/mooncat"]
    head, tail, off = [], "", 32 * len(args)
    for a in args:
        head.append(f"{off:064x}")
        enc = abi_str(a)
        tail += enc
        off += len(enc) // 2
    data = "0x12345678" + "".join(head) + tail
    strs = meta.abi_strings(data, True)
    assert args[0] in strs and args[3] in strs, strs
    m = meta.classify(strs, "Moon Cat", "MCAT")
    assert m["twitter"] == "https://x.com/mooncat", m
    assert m["telegram"] == "https://t.me/mooncat", m
    assert m["description"].startswith("The first cat"), m
    assert m["blob"].startswith(meta.C.IPFS_GATEWAY), m      # IPFS without extension: fetched to decide
    m2 = meta.classify(['{"image":"https://a.b/c.png","twitter":"@cat","website":"cat.xyz"}'], "x", "y")
    assert m2 == {"image": "https://a.b/c.png", "twitter": "https://x.com/cat", "website": "https://cat.xyz"}, m2


def test_links():
    v = lambda kind, name: Venue(kind, name, "0x" + "1" * 40)
    assert links.pad_of(v("kleos", "Kleos")) == "KLEOS"
    assert links.pad_of(v("ep4", "ElysPump v3")) == "ELYSPUMP"
    assert links.pad_of(v("ep4", "Montra")) == "MONTRA"
    assert links.pad_of(v("obol", "Obol 3")) == "OBOL"
    assert links.pad_of(v("v3", "StockiumSwap")) == "STOCKIUM"
    assert links.pad_of(v("v3", "KleosSwap")) is None
    assert links.pad_of(v("v2", "Uniswap V2")) is None
    t = "0x" + "ab" * 20
    assert links.degen_link(t) == f"https://t.me/elysiumdegen_bot?start=_{'ab' * 20}"
    assert links.based_link(t) == "https://t.me/based_eth_bot?start=r_Ramram"
    assert is_lp("UNI-V2", "Uniswap V2") and not is_lp("HELP", "Help coin")


class FakeRpc:
    def __init__(self):
        self.http = None

    async def req(self, method, params=None):
        if method == "eth_getTransactionByHash":
            return {"hash": params[0], "from": "0x" + "d" * 40, "to": "0x" + "f" * 40, "input": "0x"}
        if method == "eth_getCode":
            return "0x"
        if method == "eth_getTransactionReceipt":
            return {"logs": []}
        raise AssertionError(method)

    async def balance(self, a):
        return 12 * 10 ** 17

    async def call(self, to, data):
        return "0x" + f"{5 * 10 ** 25:064x}"      # dev holds 5% of 1B


TOKEN = "0x" + "a1" * 20
TOK = Token(TOKEN, "Moon Cat", "MCAT", 18, 10 ** 27)


class FakeDex:
    async def token(self, a):
        return TOK


class FakeMarket:
    mc = 5000.0

    async def try_snap(self, a):
        r = Route(Venue("kleos", "Kleos", "0x" + "1" * 40), probe_out=1, liquidity=10 ** 19)
        price = self.mc / 30 / 1e9
        return Snap(TOK, r, price, price * 1e9, 10, 30.0, "KLEOS")


class FakeBot:
    username = "elys_analyzer_bot"

    def __init__(self):
        self.sent = []

    async def send_message(self, **kw):
        self.sent.append(kw)
        return types.SimpleNamespace(message_id=len(self.sent), photo=None)

    send_photo = send_message


def test_deploy_and_tracker():
    store = Store(os.environ["DB_PATH"])
    feed = DeployFeed(store, FakeRpc(), FakeDex(), FakeMarket())
    store.kv_set("deploy_channel", "-100123")
    bot = FakeBot()
    feed.app = types.SimpleNamespace(bot=bot)
    # an earlier deploy by the same dev, with an ATH
    store.q("INSERT INTO deploys (token, dev, pad, ts, block, symbol, posted, ath) VALUES (?,?,?,?,?,?,?,?)",
            ("0x" + "b2" * 20, "0x" + "d" * 40, "ELYSPUMP", 1, 10, "OLD", 1, 42000))

    async def go():
        await feed._new_inner(TOKEN, "0x" + "e" * 64, 100, post=True)
    asyncio.run(go())
    assert len(bot.sent) == 1, bot.sent
    text = bot.sent[0]["text"]
    for part in ("KLEOS DEPLOY!", "Moon Cat", "$MCAT", "Deploys: 1", "Best ATH: <b>$42K</b>", "1.200 HYPE",
                 "Dev holds: <b>5.00%</b>", "MC: <b>$5K</b>"):
        assert part in text, (part, text)
    kb = bot.sent[0]["reply_markup"].inline_keyboard
    assert kb[0][0].url.startswith("https://t.me/elysiumdegen_bot?start=_a1a1")
    assert kb[1][0].url == f"https://www.thekleos.xyz/token/{TOKEN}"
    assert store.deploy(TOKEN)["posted"] == 1

    posts = []

    async def post(key, text, img, kb):
        posts.append((key, text))
    mk = FakeMarket()
    tr = Tracker(store, mk, post)

    async def run():
        await tr.check(TOKEN)          # $5K
        mk.mc = 21000
        await tr.check(TOKEN)          # crosses 10K and 20K → one alert in 20k
        mk.mc = 15000
        await tr.check(TOKEN)          # −28% → dip
    store.q("UPDATE deploys SET ts=? WHERE token=?", (__import__("time").time(), TOKEN))
    asyncio.run(run())
    keys = [k for k, _ in posts]
    assert keys == ["20k", "dip"], posts
    assert store.deploy(TOKEN)["ath"] == 21000


def test_pnl_render():
    import calls
    c = calls.Call(1, TOKEN, "Bob", 5000, 0, 1, "bob", "MCAT", 21000, 15000, 0)
    png = calls.render_pnl(c, "Moon Cat", 15000, [(1, 0, 0, 0, 5000), (2, 0, 0, 0, 21000), (3, 0, 0, 0, 15000)], 1)
    assert png[:4] == b"\x89PNG"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
