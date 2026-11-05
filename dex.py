"""Token info, quotes and swaps on Elysium (HYPE ⇄ token) across several venues.

Venue kinds (all checked on mainnet, chain 99901, 2026-11-03):
  kleos   Kleos v5 router — Kleos launches on Uniswap v4 with hooks (buy/sell tax). One call each way:
          buy(token, 0, minOut, to, deadline) payable · sell(token, amountIn, 0, minHype, to, deadline)
          quotes: quoteBuy(token, hypeIn) / quoteSell(token, tokenIn).
  v3      Uniswap V3-style pools (KleosSwap): factory.getPool + QuoterV2 + SwapRouter (exactInputSingle,
          multicall + unwrapWETH9 for sells).
  v2      Uniswap V2 routers (official Uniswap and ElysPump stack).
  boxa4   Boxa launchpad (Uniswap v4 + Boxa hook): launcher.poolKey(token) → router.buy(key, minOut, to, deadline)
          payable · router.sell(key, amountIn, minOut, to, deadline) after approve(router). Quotes: v4 Quoter.
  ep4     ElysPump launchpad v2/v3 (Uniswap v4, key = (HYPE, token, fee 0, tickSpacing 200, hook)) through the
          Universal Router: execute(V4_SWAP: SWAP_EXACT_IN_SINGLE, SETTLE_ALL, TAKE_ALL); sells go through Permit2.
          Encoding matches elysp.com's own encoder byte for byte.
  kpair   Kleos v5 tokens paired with another token (e.g. $BLOS/$BLOB): Kleos pair router does HYPE → pair → token
          in one tx. token.pairToken() (0x999b93af) names the pair; the first hop's v4 key is found on Kleos,
          ElysPump or Boxa. buy(token, key, minOut, to, dl) payable · sell(token, key, amountIn, minOut, to, dl);
          quotes return (pair amount, final amount).
  v3 venues without a quoter (StockiumSwap, Uniswap V3) quote by simulating the router call (eth_call).
Each token trades on the venue that gives the most tokens for a small probe buy. Every venue has its own
wrapped HYPE (Kleos uses 0x0269…, Uniswap uses 0xba87…), so it is stored per venue.
"""
from __future__ import annotations

import asyncio
import os
import logging
import time
from dataclasses import dataclass, field

from evm import (ADDR_RE, Rpc, RpcError, Sender, UINT_MAX, _addr, _word, dec_addr, dec_string, dec_uint,
                 dec_uint_array, encode, receipt_cost, to_wei)

# ERC-20
NAME, SYMBOL, DECIMALS, TOTAL_SUPPLY = "0x06fdde03", "0x95d89b41", "0x313ce567", "0x18160ddd"
BALANCE_OF, ALLOWANCE, APPROVE, TRANSFER = "0x70a08231", "0xdd62ed3e", "0x095ea7b3", "0xa9059cbb"
# Uniswap V2
WETH, FACTORY, GET_PAIR = "0xad5c4648", "0xc45a0155", "0xe6a43905"
GET_AMOUNTS_OUT = "0xd06ca61f"
SWAP_ETH_FOR_TOKENS = "0xb6f9de95"   # swapExactETHForTokensSupportingFeeOnTransferTokens
SWAP_TOKENS_FOR_ETH = "0x791ac947"   # swapExactTokensForETHSupportingFeeOnTransferTokens
# Uniswap V3
GET_POOL = "0x1698ee82"              # getPool(address,address,uint24)
QUOTE_V2_SINGLE = "0xc6a5026a"       # QuoterV2.quoteExactInputSingle((tokenIn,tokenOut,amountIn,fee,sqrtLimit))
EXACT_INPUT_SINGLE = "0x414bf389"    # SwapRouter.exactInputSingle((in,out,fee,recipient,deadline,amountIn,minOut,limit))
MULTICALL = "0xac9650d8"             # multicall(bytes[])
UNWRAP_WETH9 = "0x49404b7c"          # unwrapWETH9(uint256,address)
V3_FEES = (10000, 3000, 500, 100)
# Kleos v5 router
K_BUY, K_SELL = "0x5914c7cf", "0xb97018f0"
K_QUOTE_BUY, K_QUOTE_SELL = "0xc5e358b2", "0x91500c48"
K_WHYPE = "0x46e25312"
# Kleos v4 router and the KLEOS-pairs router (HYPE in, routed to the pair coin inside): native HYPE, no WHYPE
K4_BUY, K4_SELL = "0xb3ffb760", "0x883c18b3"           # buy(token,minOut,to,deadline) / sell(token,amountIn,minOut,to,deadline)
K4_QUOTE_BUY, K4_QUOTE_SELL = "0x0d7a94f6", "0xd98b2f5c"  # quoteBuy(token,hypeIn) / quoteSell(token,amountIn)
# ELYXA launchpad (elyxa.trade app config, 2026-11-05): native HYPE, bytes32 referral code (0)
EX_BUY, EX_SELL = "0x0058deaa", "0x8a92a0fe"    # buy(token,minOut,code,to) / sell(token,amount,minOut,code,to)
EX_QUOTE = "0xd7f4bf45"                          # quote(token,isBuy,amountIn,trader) -> (out, fee)
# Obol pads (obol.world): buy/sell on the curve, buyPool/sellPool after graduation; HYPE in, recipient = sender
OB_BUY, OB_SELL = "0x1622dbe4", "0x92cdbac5"    # buy(token,hypeIn,minOut,deadline) / sell(token,amountIn,minOut,deadline)
OB_BUY_POOL, OB_SELL_POOL = "0x73491ee7", "0xddc2953d"
OB_QUOTE_POOL = "0x42a5a006"                     # quotePool(token,isBuy,amount); quoteBuy/quoteSell = K4_QUOTE_*

# Uniswap v4
V4_QUOTE = "0xaa9d21cb"              # V4Quoter.quoteExactInputSingle((key,zeroForOne,amount,hookData))
UR_EXECUTE = "0x3593564c"            # UniversalRouter.execute(bytes,bytes[],uint256)
PERMIT2 = "0x000000000022d473030f116ddee9f6b43ac78ba3"
PERMIT2_APPROVE = "0x87517c45"       # approve(token, spender, uint160, uint48)
PERMIT2_ALLOWANCE = "0x927da105"     # allowance(owner, token, spender) → (uint160, uint48, uint48)
BOXA_POOL_KEY = "0x55d1cb60"         # launcher.poolKey(address)
BOXA_BUY, BOXA_SELL = "0x6af95027", "0xb261ebf3"
EP_TICK_SPACING = 200
# Kleos pair router
KP_BUY, KP_SELL = "0x4ad72713", "0xfe7ba390"
KP_QUOTE_BUY, KP_QUOTE_SELL = "0xd0485b1a", "0x2cb22bf2"
K_PAIR_TOKEN, K_HOOK = "0x999b93af", "0x7f5a7c7b"   # on Kleos v5 tokens

ZERO = "0x" + "0" * 40
PROBE = to_wei("0.1")
import os as _os
CACHE_TTL = float(_os.getenv("ROUTE_CACHE_SEC", "1800"))     # how long a token's venue is remembered
ROUTE_GRACE = float(_os.getenv("ROUTE_GRACE_SEC", "0.7"))    # after the first pool is found, wait this long for others
ROUTE_DEADLINE = float(_os.getenv("ROUTE_DEADLINE_SEC", "5"))  # whole search, at most  # HYPE used to compare venues
log = logging.getLogger("dex")

# Defaults verified on Elysium mainnet; override with ROUTERS in .env
# v3: factory/router/quoter ("-" = no quoter, quote by simulation)
# boxa4: launcher/router/quoter · ep4: hook/universal router/quoter
# Kleos addresses: from the thekleos.xyz app config (v5 = current launchpad, v5Older, v4, pairs), 2026-11-05
DEFAULT_VENUES = ("kleos:Kleos=0x15c7ee481ce8c57461bc8620adc5fb784374c240,"
                  "kleos:Kleos v5 old=0xb15d5a0159fc11bf1a1f9e383b0de76440ec6fc3,"
                  "kleos4:Kleos v4=0xba0b2699a9d21a2a138c494db82dfd2ffce3344c,"
                  "kpair:Kleos pairs=0xfd5d7bf62a315495a59715c333bd1311a3a1f7ed,"
                  "kpair:Kleos pairs old=0x9428d860fe65c585704cca1c3f2f02eecf12ee0d,"
                  # hop router for v5 coins paired with KNTQ (HYPE→KNTQ on Uniswap v4, then the coin)
                  "kpair:Kleos KNTQ pairs=0xb755388a004db8008a1f6e88274090f02fc5ddb3,"
                  "kleos4:Kleos KLEOS pairs=0x819741d02bafa76f0eedf9ee8edab2a3eccd37de,"
                  "elyxa:ELYXA=0x1cb9e9093c940d027c08c63bf7321e146ba0656c,"
                  # Montra (montramarkets.xyz/docs/contracts): plain Uniswap v4 pools with the Montra launch hook on
                  # the canonical PoolManager, traded through Universal Router 0x1b34 / V4 quoter 0x379f
                  "ep4:Montra=0x6b1216a40b65c83e9c9e18a562c446697b8cf8ec/0x1b341fdd55d56f8e08409a2c25ddfad93eb03c1a"
                  "/0x379f556c5a748bd7cc9baf0f7a344094ac1814f5/8388608,"
                  # Obol: current v4 pad first, then older pads whose coins still trade (obol.world /api/coins)
                  "obol:Obol=0x4de07902f28d00736e29aa5739dea2d4ce98aacc,"
                  "obol:Obol 2=0xfee823be55ee12b8c3a76aff0bd69470afaf2348,"
                  "obol:Obol 3=0x7d1904bfb2472a0ef97d886139a4236060e064ab,"
                  "obol:Obol 4=0x76d615181875cb7df2248197b9e573078df52acc,"
                  "obol:Obol 5=0xd096377596efd99e3a80431e31cea857c226eacc,"
                  "obol:Obol 6=0x84941cb4495ff9c79af81ebcd46124809e65f7ec,"
                  "obol:Obol 7=0x51d9243d87ad13d7eca55ab57357b2b5fc8241f2,"
                  "v3:KleosSwap=0x809ff79945fffbf9b8df33bb3b62872ceb2efaf1/0x03c291d55cc178b8fc3e20607125b8f6e9d12687"
                  "/0x18c139815b72f06a89e5e7f811427cd96b61caff,"
                  "boxa4:Boxa=0x00d4c904f74f74c6920c95e3ee807ab3c52dd205/0x7d812380c241b331d753e8eef1cefdea4e539496"
                  "/0x1ad2186a2734dc8715dea55967df823fd6e6fea4,"
                  "v3:Boxa V3=0xe83aabbd2e326ceaae3bdcb605b3af94ac3cf883/0x3398765165b9aebf317036bf922f95f9bc6613c0"
                  "/0x93900d707a3b283ccd2e998d233d2db6a326b73f,"
                  "v3:Boxa Stocks=0xd5a7c10820446c58868a204b8979b8cc9cb2a1a3/0x03c820b17f17ea86449f6cf700788c97511566c4"
                  "/0x4f7dc08e8cc4579763bdec97dd9753086e4f1e3f,"
                  "ep4:ElysPump=0xe11cafa5901b0aab51eb78251488400f9e4320cc/0x751cefb96ac88db5327d39e0fbdd5d882d80b430"
                  "/0x4861741785a8319a935741d0b570cafc33c4ca6b,"
                  "ep4:ElysPump v3=0xbacf71bf02b8cfe171853c5ec4a060ba2ac1e0cc/0x1b341fdd55d56f8e08409a2c25ddfad93eb03c1a"
                  "/0x379f556c5a748bd7cc9baf0f7a344094ac1814f5,"
                  "v3:StockiumSwap=0x4f66184a9ffabb7f7071d590c2c48dedf41676f4/0xf7d8d5f9b9125b3d29c5778df5e54c1bbe77f224/-,"
                  "v3:Uniswap V3=0x367f85886af509e364c8190862bc714555fefc3e/0x10c2b8f290f90f373b9b86c14cdf532367d1a22e/-,"
                  "v2:Uniswap V2=0xe783de89a7f0408687f051e3e6d0beb62719ebad,"
                  "v2:ElysPump V2=0x3320b5685c1c4a0d2a5b0ca30dcee095142bf076,"
                  # any other pool on the canonical v4 PoolManager (Ascend, RONS v4 …): key found from the token's trades
                  "v4any:Uniswap V4=0xe2e105d3f7209a9da11f83ccf3e7398a753823f1/0x1b341fdd55d56f8e08409a2c25ddfad93eb03c1a"
                  "/0x379f556c5a748bd7cc9baf0f7a344094ac1814f5")


class NoPool(RuntimeError):
    pass


@dataclass
class Token:
    address: str
    name: str
    symbol: str
    decimals: int
    supply: int


@dataclass
class Venue:
    kind: str                    # kleos | kpair | v3 | v2 | boxa4 | ep4
    name: str
    router: str
    factory: str = ""
    quoter: str = ""
    whype: str = ""
    launcher: str = ""           # boxa4
    pool_fee: int = 0            # ep4: PoolKey fee (0x800000 = dynamic fee set by the hook)
    hook: str = ""               # ep4


@dataclass
class Route:
    venue: Venue
    fee: int = 0                 # v3 fee tier
    pool: str = ""               # v2 pair / v3 pool, "" for kleos
    probe_out: int = 0           # tokens for PROBE HYPE
    liquidity: int | None = None  # wrapped HYPE in the pool, if known
    key: str = ""                # v4 PoolKey, abi-encoded (5 words)
    via: str = ""                # kpair: the pair token the trade goes through

    def price(self, decimals: int) -> float:
        """HYPE per 1 whole token (from the probe quote, includes fees/tax)."""
        if not self.probe_out:
            return 0.0
        return (PROBE / 1e18) / (self.probe_out / 10 ** decimals)


def parse_routers(spec: str) -> list[Venue]:
    """'kind:Name=addr[/addr/addr], …'. kind defaults to v2 (backwards compatible: 'Name=0x…').
    v3 takes factory/router/quoter."""
    out = []
    for i, part in enumerate(x.strip() for x in spec.split(",") if x.strip()):
        kind = "v2"
        if ":" in part.split("=")[0]:
            kind, _, part = part.partition(":")
            kind = kind.strip().lower()
        name, _, addrs = part.rpartition("=")
        a = [x.strip() for x in addrs.split("/")]
        pool_fee = 0
        if kind == "ep4" and len(a) == 4 and a[3].isdigit():
            pool_fee = int(a.pop())
        if kind not in ("v2", "v3", "kleos", "kleos4", "elyxa", "obol", "kpair", "boxa4", "ep4", "v4any") or not all(ADDR_RE.fullmatch(x) or x == "-" for x in a):
            raise ValueError(f"bad venue {part!r}")
        name = name.strip() or f"DEX{i + 1}"
        q = lambda x: "" if x == "-" else x
        if kind in ("v3", "boxa4", "ep4", "v4any"):
            if len(a) != 3 or a[1] == "-":
                raise ValueError(f"{kind} venue {name}: need 3 addresses")
            v = Venue(kind, name, a[1], quoter=q(a[2]))
            if kind in ("v3", "v4any"):
                v.factory = a[0]          # v4any: the PoolManager
            elif kind == "boxa4":
                v.launcher = a[0]
            else:
                v.hook = a[0]
                v.pool_fee = pool_fee
            out.append(v)
        else:
            out.append(Venue(kind, name, a[0]))
    return out


def encode_bytes_array(selector: str, items: list[str]) -> str:
    """selector + abi.encode(bytes[]) for multicall."""
    blobs = [x[2:] if x.startswith("0x") else x for x in items]
    head, tail, off = [], [], 32 * len(blobs)
    for b in blobs:
        head.append(_word(off))
        n = len(b) // 2
        padded = b + "0" * ((64 - len(b) % 64) % 64)
        tail.append(_word(n) + padded)
        off += 32 + len(padded) // 2
    return selector + _word(32) + _word(len(blobs)) + "".join(head) + "".join(tail)


def bl(h: str) -> str:
    """ABI `bytes` body: length word + right-padded data (hex without 0x)."""
    h = h[2:] if h.startswith("0x") else h
    return _word(len(h) // 2) + h + "0" * ((64 - len(h) % 64) % 64)


def ep_key(token: str, hook: str, fee: int = 0) -> str:
    """v4 PoolKey of a launchpad pool: (native HYPE, token, fee, tickSpacing 200, hook). ElysPump: fee 0;
    Montra: 0x800000 (dynamic fee)."""
    return _word(0) + _addr(token) + _word(fee) + _word(EP_TICK_SPACING) + _addr(hook)


def v4_quote_data(key: str, buy: bool, amount: int) -> str:
    return V4_QUOTE + _word(32) + key + _word(1 if buy else 0) + _word(amount) + _word(256) + _word(0)


def ur_v4_swap(key: str, token: str, buy: bool, amount: int, min_out: int, deadline: int) -> str:
    """UniversalRouter.execute with one V4_SWAP: SWAP_EXACT_IN_SINGLE, SETTLE_ALL, TAKE_ALL (as elysp.com)."""
    native = "0" * 64
    ci, co = (native, _addr(token)) if buy else (_addr(token), native)
    p0 = _word(32) + key + _word(1 if buy else 0) + _word(amount) + _word(min_out) + _word(288) + _word(0)
    ps = [p0, ci + _word(amount), co + _word(min_out)]
    offs, o = [], 96
    for p in ps:
        offs.append(_word(o))
        o += 32 + len(p) // 2
    arr = _word(3) + "".join(offs) + "".join(bl(p) for p in ps)
    inp = _word(64) + _word(128) + bl("060c0f") + arr
    return UR_EXECUTE + _word(96) + _word(160) + _word(deadline) + bl("10") + _word(1) + _word(32) + bl(inp)


# ---- any Uniswap v4 pool (Ascend and other launchpads we don't know): key found from the token's own trades
V4_SWAP_TOPIC = "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
CANON_WHYPE = "0xba876237b0f021a6c6d852d6c26e107997cc5e81"
ADDRESS_THIS, MSG_SENDER = 2, 1                 # Universal Router recipient constants
CONTRACT_BALANCE = 1 << 255
A_SWAP_IN_SINGLE, A_SETTLE, A_SETTLE_ALL, A_TAKE, A_TAKE_ALL = "06", "0b", "0c", "0e", "0f"
C_WRAP_ETH, C_UNWRAP_WETH, C_V4_SWAP = "0b", "0c", "10"


def _keccak(hexdata: str) -> str:
    from eth_utils import keccak
    return "0x" + keccak(bytes.fromhex(hexdata)).hex().removeprefix("0x")


def v4_quote_any(key: str, zero_for_one: bool, amount: int) -> str:
    return V4_QUOTE + _word(32) + key + _word(int(zero_for_one)) + _word(amount) + _word(256) + _word(0)


def _bytes_array(items: list[str]) -> str:
    offs, o = [], 32 * len(items)
    for p in items:
        offs.append(_word(o))
        o += len(bl(p)) // 2
    return _word(len(items)) + "".join(offs) + "".join(bl(p) for p in items)


def _ur(commands: str, inputs: list[str], deadline: int) -> str:
    cmds = bl(commands)
    return UR_EXECUTE + _word(96) + _word(96 + len(cmds) // 2) + _word(deadline) + cmds + _bytes_array(inputs)


def _v4_swap_input(actions: str, params: list[str]) -> str:
    """V4_SWAP command input: abi.encode(bytes actions, bytes[] params)."""
    head_actions = bl(actions)
    return _word(64) + _word(64 + len(head_actions) // 2) + head_actions + _bytes_array(params)


def ur_v4_any(key: str, token: str, hype: str, buy: bool, amount: int, min_out: int, deadline: int) -> str:
    """Universal Router swap through any v4 pool. hype = '0x0…' (native) or WHYPE: for WHYPE pools the router
    wraps the HYPE sent with the tx before the swap and unwraps the proceeds of a sale."""
    hype0 = int(key[:64], 16) == int(hype, 16)
    zfo = hype0 if buy else not hype0
    swap = _word(32) + key + _word(int(zfo)) + _word(amount) + _word(min_out) + _word(288) + _word(0)
    native = int(hype, 16) == 0
    if native:   # same as elysp.com: SWAP, SETTLE_ALL (pays the native HYPE sent with the tx), TAKE_ALL
        cin, cout = (hype, token) if buy else (token, hype)
        params = [swap, _addr(cin) + _word(amount), _addr(cout) + _word(min_out)]
        return _ur(C_V4_SWAP, [_v4_swap_input(A_SWAP_IN_SINGLE + A_SETTLE_ALL + A_TAKE_ALL, params)], deadline)
    if buy:      # WRAP_ETH into the router, swap paying WHYPE from the router, take the token to the buyer
        wrap = _word(ADDRESS_THIS) + _word(CONTRACT_BALANCE)
        params = [swap, _addr(hype) + _word(0) + _word(0), _addr(token) + _word(min_out)]   # SETTLE(cur, OPEN_DELTA, payerIsUser=false)
        return _ur(C_WRAP_ETH + C_V4_SWAP, [wrap, _v4_swap_input(A_SWAP_IN_SINGLE + A_SETTLE + A_TAKE_ALL, params)],
                   deadline)
    # sell: user's token via Permit2, WHYPE taken into the router, unwrapped to the seller
    params = [swap, _addr(token) + _word(amount), _addr(hype) + _word(ADDRESS_THIS) + _word(0)]   # TAKE(cur, router, OPEN_DELTA)
    unwrap = _word(MSG_SENDER) + _word(min_out)
    return _ur(C_V4_SWAP + C_UNWRAP_WETH, [_v4_swap_input(A_SWAP_IN_SINGLE + A_SETTLE_ALL + A_TAKE, params), unwrap],
               deadline)


class Dex:
    def __init__(self, rpc: Rpc, venues: list[Venue]):
        self.rpc, self.venues = rpc, venues
        self._venue_cache: dict[str, tuple[float, str, int, str, str]] = {}  # token → (ts, venue name, fee, pool, key)
        self._meta: dict[str, tuple[str, str, int]] = {}      # token → (name, symbol, decimals)
        self._supply: dict[str, tuple[float, int]] = {}       # token → (ts, total supply)
        self._v4keys: dict[str, tuple[float, tuple | None]] = {}   # token → (ts, (key, hype currency) or None)
        self._v4file = os.getenv("V4_KEYS_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "v4keys.json"))
        try:   # pools found earlier survive restarts
            import json
            with open(self._v4file) as f:
                for t, (k, h) in json.load(f).items():
                    self._v4keys[t] = (time.monotonic(), (k, h))
        except (OSError, ValueError):
            pass
        self._v4jobs: dict[str, asyncio.Task] = {}

    async def _check_venue(self, v: Venue):
        if v.kind == "v2":
            v.whype = dec_addr(await self.rpc.call(v.router, WETH))
            v.factory = dec_addr(await self.rpc.call(v.router, FACTORY))
        elif v.kind == "v3":
            v.whype = dec_addr(await self.rpc.call(v.router, "0x4aa4a4fc"))  # WETH9()
        elif v.kind == "kleos":
            v.whype = dec_addr(await self.rpc.call(v.router, K_WHYPE))
        elif v.kind in ("kpair", "kleos4", "elyxa", "obol"):
            if len(await self.rpc.code(v.router)) < 4:
                raise RuntimeError("no code")
        else:  # v4 pools: check the contracts exist
            if len(await self.rpc.code(v.router)) < 4 or len(await self.rpc.code(v.quoter)) < 4:
                raise RuntimeError("no code")

    async def init(self):
        """Checks every venue. One that failed because a node didn't answer (not because the contract is missing)
        is retried in the background every minute — a slow node at start-up must not drop a venue for good."""
        ok, retry = [], []
        results = await asyncio.gather(*(self._check_venue(v) for v in self.venues), return_exceptions=True)
        for v, res in zip(self.venues, results):
            if res is None:
                ok.append(v)
            elif isinstance(res, RuntimeError) and str(res) == "no code":
                log.error("venue %s %s unusable: no contract", v.name, v.router)
            else:
                log.warning("venue %s %s: %s — retrying in the background", v.name, v.router, res)
                retry.append(v)
        if not ok and not retry:
            raise SystemExit("no usable venue in ROUTERS")
        self.venues = ok
        if retry:
            asyncio.get_running_loop().create_task(self._retry_venues(retry))

    async def _retry_venues(self, pending: list[Venue]):
        while pending:
            await asyncio.sleep(60)
            for v in list(pending):
                try:
                    await self._check_venue(v)
                except RuntimeError as ex:
                    if str(ex) == "no code":
                        pending.remove(v)
                    continue
                except Exception:
                    continue
                pending.remove(v)
                self.venues.append(v)
                log.info("venue %s is back", v.name)

    async def token(self, addr: str) -> Token:
        """Name/symbol/decimals cached for good, supply for a minute; the calls run in parallel."""
        key = addr.lower()
        meta = self._meta.get(key)
        sup = self._supply.get(key)
        if meta and sup and time.monotonic() - sup[0] < 60:
            return Token(addr, *meta, sup[1])

        async def call(sel):
            try:
                return await self.rpc.call(addr, sel)
            except (RpcError, IndexError) as ex:
                return ex
        if meta:
            r = await call(TOTAL_SUPPLY)
            supply = 0 if isinstance(r, Exception) else dec_uint(r)
            self._supply[key] = (time.monotonic(), supply)
            return Token(addr, *meta, supply)
        r_dec, r_name, r_sym, r_sup = await asyncio.gather(call(DECIMALS), call(NAME), call(SYMBOL), call(TOTAL_SUPPLY))
        try:
            if isinstance(r_dec, Exception):
                raise r_dec
            dec = dec_uint(r_dec)
        except (RpcError, IndexError):
            raise NoPool("это не токен (нет decimals)") from None

        def s(r):
            try:
                return "?" if isinstance(r, Exception) else (dec_string(r) or "?")
            except (ValueError, IndexError):
                return "?"
        try:
            supply = 0 if isinstance(r_sup, Exception) else dec_uint(r_sup)
        except IndexError:
            supply = 0
        meta = (s(r_name).strip()[:40], s(r_sym).strip()[:16], dec)
        self._meta[key] = meta
        self._supply[key] = (time.monotonic(), supply)
        return Token(addr, *meta, supply)

    async def balance_of(self, token: str, owner: str) -> int:
        return dec_uint(await self.rpc.call(token, encode(BALANCE_OF, owner)))

    # ------------------------------------------------------------ quotes
    async def _sim(self, frm: str, to: str, data: str, value: int = 0) -> str:
        return await self.rpc.req("eth_call", [{"from": frm, "to": to, "data": data, "value": hex(value)}, "latest"])

    async def quote_buy(self, r: Route, token: str, hype_in: int) -> int:
        v = r.venue
        if v.kind == "kleos":
            return dec_uint(await self.rpc.call(v.router, encode(K_QUOTE_BUY, token, hype_in)))
        if v.kind == "kleos4":
            return dec_uint(await self.rpc.call(v.router, encode(K4_QUOTE_BUY, token, hype_in)))
        if v.kind == "elyxa":
            return dec_uint(await self.rpc.call(v.router, encode(EX_QUOTE, token, 1, hype_in, ZERO)))
        if v.kind == "obol":
            if r.key == "pool":
                return dec_uint(await self.rpc.call(v.router, encode(OB_QUOTE_POOL, token, 1, hype_in)))
            return dec_uint(await self.rpc.call(v.router, encode(K4_QUOTE_BUY, token, hype_in)))
        if v.kind == "v4any":
            hype0 = int(r.key[:64], 16) == int(r.via, 16)
            return dec_uint(await self.rpc.call(v.quoter, v4_quote_any(r.key, hype0, hype_in)))
        if v.kind in ("boxa4", "ep4"):
            return dec_uint(await self.rpc.call(v.quoter, v4_quote_data(r.key, True, hype_in)))
        if v.kind == "kpair":
            return dec_uint(await self.rpc.call(v.router, KP_QUOTE_BUY + _addr(token) + r.key + _word(hype_in)), 1)
        if v.kind == "v3" and not v.quoter:  # simulate the swap from the WHYPE contract (it holds native HYPE)
            data = encode(EXACT_INPUT_SINGLE, v.whype, token, r.fee, v.whype, int(time.time()) + 600, hype_in, 0, 0)
            return dec_uint(await self._sim(v.whype, v.router, data, hype_in))
        if v.kind == "v3":
            return dec_uint(await self.rpc.call(v.quoter, encode(QUOTE_V2_SINGLE, v.whype, token, hype_in, r.fee, 0)))
        return dec_uint_array(await self.rpc.call(v.router, encode(GET_AMOUNTS_OUT, hype_in, [v.whype, token])))[-1]

    async def quote_sell(self, r: Route, token: str, amount: int, owner: str | None = None) -> int:
        """owner is needed only for v3 venues without a quoter (simulated from the holder's address)."""
        v = r.venue
        if v.kind == "kleos":
            return dec_uint(await self.rpc.call(v.router, encode(K_QUOTE_SELL, token, amount)))
        if v.kind == "kleos4":
            return dec_uint(await self.rpc.call(v.router, encode(K4_QUOTE_SELL, token, amount)))
        if v.kind == "elyxa":
            return dec_uint(await self.rpc.call(v.router, encode(EX_QUOTE, token, 0, amount, owner or ZERO)))
        if v.kind == "obol":
            if r.key == "pool":
                return dec_uint(await self.rpc.call(v.router, encode(OB_QUOTE_POOL, token, 0, amount)))
            return dec_uint(await self.rpc.call(v.router, encode(K4_QUOTE_SELL, token, amount)))
        if v.kind == "v4any":
            hype0 = int(r.key[:64], 16) == int(r.via, 16)
            return dec_uint(await self.rpc.call(v.quoter, v4_quote_any(r.key, not hype0, amount)))
        if v.kind in ("boxa4", "ep4"):
            return dec_uint(await self.rpc.call(v.quoter, v4_quote_data(r.key, False, amount)))
        if v.kind == "kpair":
            return dec_uint(await self.rpc.call(v.router, KP_QUOTE_SELL + _addr(token) + r.key + _word(amount)), 1)
        if v.kind == "v3" and not v.quoter:
            if owner:
                try:
                    data = encode(EXACT_INPUT_SINGLE, token, v.whype, r.fee, owner, int(time.time()) + 600, amount, 0, 0)
                    return dec_uint(await self._sim(owner, v.router, data))
                except (RpcError, IndexError):
                    pass  # no allowance yet → spot estimate
            # spot estimate from the pool's balances is not exact for v3; use the buy quote's inverse
            out = await self.quote_buy(r, token, PROBE)
            return amount * PROBE // out if out else 0
        if v.kind == "v3":
            return dec_uint(await self.rpc.call(v.quoter, encode(QUOTE_V2_SINGLE, token, v.whype, amount, r.fee, 0)))
        return dec_uint_array(await self.rpc.call(v.router, encode(GET_AMOUNTS_OUT, amount, [token, v.whype])))[-1]

    async def _routes_at(self, v: Venue, token: str) -> list[Route]:
        try:
            if v.kind == "obol":  # on the curve: quoteBuy; graduated coins answer quotePool
                for key in ("", "pool"):
                    r = Route(v, key=key)
                    try:
                        r.probe_out = await self.quote_buy(r, token, PROBE)
                    except (RpcError, IndexError):
                        continue
                    if r.probe_out:
                        return [r]
                return []
            if v.kind == "kleos":
                # coins paired with another token (KNTQ, BLOB …) quote here but buying reverts NoHypeRoute():
                # those go through a hop router (kpair)
                try:
                    if dec_addr(await self.rpc.call(token, K_PAIR_TOKEN)) != ZERO:
                        return []
                except (RpcError, IndexError, ValueError):
                    pass
            if v.kind in ("kleos", "kleos4", "elyxa"):
                r = Route(v)
                r.probe_out = await self.quote_buy(r, token, PROBE)
                return [r] if r.probe_out else []
            if v.kind == "kpair":
                pair = dec_addr(await self.rpc.call(token, K_PAIR_TOKEN))
                if pair == ZERO:
                    return []
                for key in await self._hype_keys(pair):
                    r = Route(v, key=key, via=pair)
                    try:
                        r.probe_out = await self.quote_buy(r, token, PROBE)
                    except (RpcError, IndexError):
                        continue
                    if r.probe_out:
                        return [r]
                return []
            if v.kind == "v4any":
                found = await self.v4_key(token, v.factory)
                if not found:
                    return []
                r = Route(v, key=found[0], via=found[1])     # via = HYPE currency of the pool (0x0 or WHYPE)
                r.probe_out = await self.quote_buy(r, token, PROBE)
                return [r] if r.probe_out else []
            if v.kind in ("boxa4", "ep4"):
                if v.kind == "boxa4":
                    raw = await self.rpc.call(v.launcher, encode(BOXA_POOL_KEY, token))
                    key = raw[2:2 + 64 * 5]
                    if len(key) != 320 or int(key[64:128], 16) != int(token, 16):
                        return []
                else:
                    key = ep_key(token, v.hook, v.pool_fee)
                r = Route(v, key=key)
                r.probe_out = await self.quote_buy(r, token, PROBE)
                return [r] if r.probe_out else []
            if v.kind == "v2":
                pair = dec_addr(await self.rpc.call(v.factory, encode(GET_PAIR, token, v.whype)))
                if pair == ZERO:
                    return []
                r = Route(v, pool=pair)
                r.liquidity = await self.balance_of(v.whype, pair)
                r.probe_out = await self.quote_buy(r, token, PROBE)
                return [r] if r.probe_out else []
            async def tier(fee):  # all fee tiers at once instead of one after another
                try:
                    pool = dec_addr(await self.rpc.call(v.factory, encode(GET_POOL, token, v.whype, fee)))
                    if pool == ZERO:
                        return None
                    r = Route(v, fee=fee, pool=pool)
                    r.liquidity = await self.balance_of(v.whype, pool)
                    if not r.liquidity:
                        return None
                    r.probe_out = await self.quote_buy(r, token, PROBE)
                    return r if r.probe_out else None
                except (RpcError, IndexError, ValueError):
                    return None
            return [r for r in await asyncio.gather(*(tier(f) for f in V3_FEES)) if r]
        except (RpcError, IndexError, ValueError):
            return []

    def _hype_addrs(self) -> set[str]:
        return {CANON_WHYPE, "0x" + "0" * 40} | {v.whype.lower() for v in self.venues if v.whype}

    @staticmethod
    def _key_candidates(data: str, token: str, hypes: set[str]):
        """PoolKeys (currency0, currency1, fee, tickSpacing, hooks) spelled out in calldata, at any 4-byte alignment
        (nested router calls shift the words by the 4-byte selectors)."""
        d = data[2:] if data.startswith("0x") else data
        tok = token[2:].lower().rjust(64, "0")
        seen = set()
        for start in range(0, 64, 8):
            words = [d[i:i + 64] for i in range(start, len(d) - 63, 64)]
            for i in range(len(words) - 4):
                c0, c1, fee, ts, hk = words[i:i + 5]
                if tok not in (c0, c1) or c0[:24] != "0" * 24 or c1[:24] != "0" * 24 or hk[:24] != "0" * 24:
                    continue
                other = "0x" + (c1 if c0 == tok else c0)[24:]
                if other not in hypes or int(fee, 16) >= 1 << 24 or not 0 < int(ts, 16) < 1 << 15:
                    continue
                key = c0 + c1 + fee + ts + hk
                if key not in seen:
                    seen.add(key)
                    yield key, other

    async def v4_key(self, token: str, manager: str, wait: float = 4.0):
        """(PoolKey, HYPE currency) of the token's v4 pool on this PoolManager, found from its recent trades:
        a trade's receipt has the pool's Swap event (pool id), its calldata has the PoolKey — keccak(key) == id.
        Cached; a search that takes longer than `wait` keeps going in the background for the next request."""
        token = token.lower()
        c = self._v4keys.get(token)
        if c and (c[1] is not None or time.monotonic() - c[0] < 45):
            return c[1]
        job = self._v4jobs.get(token)
        if job is None or job.done():
            job = self._v4jobs[token] = asyncio.ensure_future(self._find_v4_key(token, manager))
        try:
            return await asyncio.wait_for(asyncio.shield(job), wait)
        except asyncio.TimeoutError:
            return None

    async def _find_v4_key(self, token: str, manager: str):
        res = None
        try:
            import logscan
            head = int(await self.rpc.req("eth_blockNumber"), 16)
            logs, _ = await logscan.scan_back(self.rpc, {"address": token, "topics": [TRANSFER_TOPIC]}, head,
                                              lambda ls: len(ls) >= 4, 3_000_000)
            hypes = self._hype_addrs()
            all_ids: set[str] = set()
            for lg in reversed(logs[-10:]):
                txh = lg["transactionHash"]
                try:
                    tx, rc = await asyncio.gather(self.rpc.req("eth_getTransactionByHash", [txh]),
                                                  self.rpc.req("eth_getTransactionReceipt", [txh]))
                except RpcError:
                    continue
                ids = {x["topics"][1] for x in (rc or {}).get("logs") or []
                       if x.get("address", "").lower() == manager.lower() and (x.get("topics") or [""])[0] == V4_SWAP_TOPIC}
                if not ids:
                    continue
                all_ids.update(ids)
                for key, hype in self._key_candidates((tx or {}).get("input") or "", token, hypes):
                    if _keccak(key) in ids:
                        res = (key, hype)
                        break
                if res:
                    break
            if not res and all_ids:
                # bought through a router that doesn't spell out the key: read the pool's Initialize event instead
                res = await self._key_from_initialize(manager, all_ids, token, hypes, head)
        except Exception as ex:
            log.info("v4 key %s: %s", token, ex)
        self._v4keys[token] = (time.monotonic(), res)
        if res:
            self._save_v4keys()
            log.info("v4 pool of %s: hook 0x%s, %s", token, res[0][-40:], "native HYPE" if int(res[1], 16) == 0 else "WHYPE")
        return res

    async def _key_from_initialize(self, manager: str, ids: set[str], token: str, hypes: set[str], head: int):
        """PoolManager.Initialize(id, currency0, currency1, fee, tickSpacing, hooks, …) of one of these pools,
        searched backwards from the head (newest first)."""
        import logscan
        from eth_utils import keccak
        topic = "0x" + keccak(text="Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)").hex().removeprefix("0x")
        tok = token.lower()
        for pid in ids:
            logs, _ = await logscan.scan_back(self.rpc, {"address": manager, "topics": [topic, pid]}, head,
                                              lambda ls: bool(ls), int(os.getenv("V4_INIT_SEARCH_BLOCKS", "8000000")),
                                              conc=8)
            for lg in logs:
                t = lg.get("topics") or []
                if len(t) < 4:
                    continue
                c0, c1 = "0x" + t[2][-40:].lower(), "0x" + t[3][-40:].lower()
                if tok not in (c0, c1):
                    continue
                other = c1 if c0 == tok else c0
                if other not in hypes:
                    continue
                d = lg["data"][2:]
                key = t[2][2:].rjust(64, "0") + t[3][2:].rjust(64, "0") + d[0:64] + d[64:128] + d[128:192]
                if _keccak(key) == pid:
                    return key, other
        return None

    def _save_v4keys(self):
        try:
            import json
            data = {t: list(v[1]) for t, v in self._v4keys.items() if v[1]}
            with open(self._v4file + ".tmp", "w") as f:
                json.dump(data, f)
            os.replace(self._v4file + ".tmp", self._v4file)
        except OSError as ex:
            log.debug("v4 keys save: %s", ex)

    def v4_searching(self, token: str) -> bool:
        job = self._v4jobs.get(token.lower())
        return bool(job and not job.done())

    async def _hype_keys(self, token: str) -> list[str]:
        """Candidate v4 PoolKeys (native HYPE, token) for a pair token: Kleos, then ElysPump, then Boxa."""
        keys = []
        try:
            if dec_addr(await self.rpc.call(token, K_PAIR_TOKEN)) == ZERO:
                hook = dec_addr(await self.rpc.call(token, K_HOOK))
                keys.append(_word(0) + _addr(token) + _word(0) + _word(EP_TICK_SPACING) + _addr(hook))
        except (RpcError, IndexError, ValueError):
            pass
        # plain Uniswap v4 pools (no hook) on the canonical PoolManager, e.g. HYPE/KNTQ: fee 1%, tickSpacing 200
        for fee, spacing in ((10000, 200), (3000, 60), (500, 10)):
            keys.append(_word(0) + _addr(token) + _word(fee) + _word(spacing) + _word(0))
        for v in self.venues:
            if v.kind == "ep4":
                keys.append(ep_key(token, v.hook, v.pool_fee))
            elif v.kind == "boxa4":
                try:
                    raw = await self.rpc.call(v.launcher, encode(BOXA_POOL_KEY, token))
                    if len(raw) >= 2 + 320 and int(raw[2 + 64:2 + 128], 16) == int(token, 16):
                        keys.append(raw[2:2 + 320])
                except (RpcError, IndexError, ValueError):
                    pass
        return keys

    async def route(self, token: str) -> Route:
        """Venue that gives the most tokens for PROBE HYPE. The winning venue is remembered for 30 minutes, so
        trades and refreshes re-check only that venue. A fresh search asks every venue at once but doesn't wait for
        the slowest: once a pool is found it waits ROUTE_GRACE more for a better one, and never longer than
        ROUTE_DEADLINE in total (a lagging public node used to hold the whole card for seconds)."""
        c = self._venue_cache.get(token.lower())
        if c and time.monotonic() - c[0] < CACHE_TTL:
            v = next((x for x in self.venues if x.name == c[1]), None)
            if v:
                for r in await self._routes_at(v, token):
                    if r.fee == c[2] and r.pool == c[3] and r.key == c[4]:
                        return r
        t0 = time.monotonic()
        self.last_route_timing = {}

        async def timed(v):
            ts = time.monotonic()
            try:
                return await self._routes_at(v, token)
            finally:
                self.last_route_timing[v.name] = time.monotonic() - ts
        tasks = {asyncio.ensure_future(timed(v)): v for v in self.venues}
        routes: list[Route] = []
        first_found = None
        pending = set(tasks)
        while pending:
            now = time.monotonic()
            limit = t0 + ROUTE_DEADLINE
            if first_found is not None:
                limit = min(limit, first_found + ROUTE_GRACE)
            if now >= limit:
                break
            done, pending = await asyncio.wait(pending, timeout=limit - now, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                try:
                    got = t.result()
                except Exception:
                    got = []
                if got:
                    routes += got
                    if first_found is None:
                        first_found = time.monotonic()
        for t in pending:
            t.cancel()
        if pending:
            log.info("route %s: skipped slow venues %s", token[:10], [tasks[t].name for t in pending])
        if not routes:
            raise NoPool("пул с HYPE не найден ни на одной площадке")
        best = max(routes, key=lambda r: r.probe_out)
        self._venue_cache[token.lower()] = (time.monotonic(), best.venue.name, best.fee, best.pool, best.key)
        return best

    async def value(self, token: str, amount: int) -> int:
        """Token amount → HYPE on the best venue (0 if none)."""
        try:
            r = await self.route(token)
            return await self.quote_sell(r, token, amount)
        except (NoPool, RpcError, IndexError):
            return 0

    # ------------------------------------------------------------ trades (caller holds sender.lock)
    async def buy(self, s: Sender, acct, token: str, hype_in: int, slippage_bps: int, on_sent=None) -> tuple[dict, int, Venue]:
        """Returns (receipt, tokens received, venue)."""
        r = await self.route(token)
        v = r.venue
        out, before = await asyncio.gather(self.quote_buy(r, token, hype_in), self.balance_of(token, acct.address))
        min_out = out * (10_000 - slippage_bps) // 10_000
        deadline = int(time.time()) + 300
        if v.kind == "kleos":
            to, data = v.router, encode(K_BUY, token, 0, min_out, acct.address, deadline)
        elif v.kind == "kleos4":
            to, data = v.router, encode(K4_BUY, token, min_out, acct.address, deadline)
        elif v.kind == "elyxa":
            to, data = v.router, encode(EX_BUY, token, min_out, 0, acct.address)
        elif v.kind == "obol":
            to, data = v.router, encode(OB_BUY_POOL if r.key == "pool" else OB_BUY, token, hype_in, min_out, deadline)
        elif v.kind == "kpair":
            to, data = v.router, KP_BUY + _addr(token) + r.key + _word(min_out) + _addr(acct.address) + _word(deadline)
        elif v.kind == "boxa4":
            to, data = v.router, BOXA_BUY + r.key + _word(min_out) + _addr(acct.address) + _word(deadline)
        elif v.kind == "ep4":
            to, data = v.router, ur_v4_swap(r.key, token, True, hype_in, min_out, deadline)
        elif v.kind == "v4any":
            to, data = v.router, ur_v4_any(r.key, token, r.via, True, hype_in, min_out, deadline)
        elif v.kind == "v3":
            to, data = v.router, encode(EXACT_INPUT_SINGLE, v.whype, token, r.fee, acct.address, deadline,
                                        hype_in, min_out, 0)
        else:
            to, data = v.router, encode(SWAP_ETH_FOR_TOKENS, min_out, [v.whype, token], acct.address, deadline)
        rc = await s.send(acct, to, data, value=hype_in, on_sent=on_sent)
        got = await self.balance_of(token, acct.address) - before
        return rc, max(got, 0), v

    async def sell(self, s: Sender, acct, token: str, amount: int, slippage_bps: int, on_sent=None) -> tuple[dict, int, Venue]:
        """Returns (swap receipt, HYPE paid out by the swap, venue)."""
        r = await self.route(token)
        v = r.venue
        deadline = int(time.time()) + 300
        if v.kind in ("ep4", "v4any"):  # Universal Router pulls tokens through Permit2
            if dec_uint(await self.rpc.call(token, encode(ALLOWANCE, acct.address, PERMIT2))) < amount:
                await s.send(acct, token, encode(APPROVE, PERMIT2, UINT_MAX))
            p2 = await self.rpc.call(PERMIT2, encode(PERMIT2_ALLOWANCE, acct.address, token, v.router))
            p2_amount, p2_exp = dec_uint(p2, 0), dec_uint(p2, 1)
            if p2_amount < amount or p2_exp < time.time() + 600:
                await s.send(acct, PERMIT2, encode(PERMIT2_APPROVE, token, v.router, 2 ** 160 - 1,
                                                   int(time.time()) + 30 * 86400))
        else:
            allowance = dec_uint(await self.rpc.call(token, encode(ALLOWANCE, acct.address, v.router)))
            if allowance < amount:
                await s.send(acct, token, encode(APPROVE, v.router, UINT_MAX))
        out = await self.quote_sell(r, token, amount, acct.address)
        min_out = out * (10_000 - slippage_bps) // 10_000
        if v.kind == "kleos":
            data = encode(K_SELL, token, amount, 0, min_out, acct.address, deadline)
        elif v.kind == "kleos4":
            data = encode(K4_SELL, token, amount, min_out, acct.address, deadline)
        elif v.kind == "elyxa":
            data = encode(EX_SELL, token, amount, min_out, 0, acct.address)
        elif v.kind == "obol":
            data = encode(OB_SELL_POOL if r.key == "pool" else OB_SELL, token, amount, min_out, deadline)
        elif v.kind == "kpair":
            data = KP_SELL + _addr(token) + r.key + _word(amount) + _word(min_out) + _addr(acct.address) + _word(deadline)
        elif v.kind == "boxa4":
            data = BOXA_SELL + r.key + _word(amount) + _word(min_out) + _addr(acct.address) + _word(deadline)
        elif v.kind == "ep4":
            data = ur_v4_swap(r.key, token, False, amount, min_out, deadline)
        elif v.kind == "v4any":
            data = ur_v4_any(r.key, token, r.via, False, amount, min_out, deadline)
        elif v.kind == "v3":
            swap = encode(EXACT_INPUT_SINGLE, token, v.whype, r.fee, v.router, deadline, amount, min_out, 0)
            unwrap = encode(UNWRAP_WETH9, min_out, acct.address)
            data = encode_bytes_array(MULTICALL, [swap, unwrap])
        else:
            data = encode(SWAP_TOKENS_FOR_ETH, amount, min_out, [token, v.whype], acct.address, deadline)
        before = await self.rpc.balance(acct.address)
        rc = await s.send(acct, v.router, data, on_sent=on_sent)
        got = await self.rpc.balance(acct.address) - before + receipt_cost(rc)
        return rc, max(got, 0), v
