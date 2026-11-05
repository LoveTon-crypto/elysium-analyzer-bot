"""Token picture, description and socials straight from the launch transaction.

Elysium memepads have no common metadata API, but every launch call carries the strings the dev typed in the
pad's form: image URL / IPFS hash, description, X, Telegram, website — or one URI of a metadata JSON. They sit
in the tx input and in the launch event data as ABI strings, so we pull every ABI-encoded string out of both
and sort them by what they look like. Works the same for Kleos, ElysPump, Boxa, Obol, ELYXA, Montra …
"""
from __future__ import annotations

import json
import logging
import re

import httpx

import config as C

log = logging.getLogger("meta")

IMG_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".avif")
URL_RE = re.compile(r"^(https?://|ipfs://|ar://)\S+$|^(www\.)?[a-z0-9-]+\.[a-z]{2,}(/\S*)?$", re.I)
CID_RE = re.compile(r"^(Qm[1-9A-HJ-NP-Za-km-z]{44}|b[a-z2-7]{58,})$")
MAX_IMG = 5_000_000


def abi_strings(hexdata: str, has_selector: bool) -> list[str]:
    """Every ABI-encoded string (length word + utf-8 bytes + zero padding) found in the data."""
    d = (hexdata or "")[2:] if (hexdata or "").startswith("0x") else (hexdata or "")
    try:
        b = bytes.fromhex(d)
    except ValueError:
        return []
    if has_selector:
        b = b[4:]
    out, seen = [], set()
    for i in range(0, len(b) - 32, 32):
        ln = int.from_bytes(b[i:i + 32], "big")
        if not 1 <= ln <= 4000:
            continue
        end = i + 32 + ln
        pad_end = i + 32 + (ln + 31) // 32 * 32
        if pad_end > len(b) or any(b[end:pad_end]):
            continue
        try:
            s = b[i + 32:end].decode("utf-8").strip()
        except UnicodeDecodeError:
            continue
        if s and s not in seen and all(ch.isprintable() or ch in "\n\r\t" for ch in s):
            seen.add(s)
            out.append(s)
    return out


def _url(s: str) -> str:
    s = s.strip()
    if s.startswith("ipfs://"):
        return C.IPFS_GATEWAY + s[7:].removeprefix("ipfs/")
    if s.startswith("ar://"):
        return "https://arweave.net/" + s[5:]
    if CID_RE.match(s):
        return C.IPFS_GATEWAY + s
    if not s.startswith("http"):
        return "https://" + s
    return s


def classify(strings: list[str], name: str, symbol: str) -> dict:
    """{'image', 'description', 'twitter', 'telegram', 'website', 'json'} from loose strings."""
    m: dict = {}
    skip = {name.strip().lower(), symbol.strip().lower()}
    texts = []
    for s in strings:
        low = s.lower()
        if low in skip:
            continue
        if s.startswith("{") and s.endswith("}"):
            try:
                j = json.loads(s)
                if isinstance(j, dict):
                    merge_json(m, j)
                    continue
            except ValueError:
                pass
        if CID_RE.match(s) or URL_RE.match(s):
            u = _url(s)
            ul = u.lower().split("?")[0]
            if "t.me/" in ul or "telegram.me/" in ul:
                m.setdefault("telegram", u)
            elif "x.com/" in ul or "twitter.com/" in ul:
                m.setdefault("twitter", u)
            elif ul.endswith(IMG_EXT) or "/image" in ul:
                m.setdefault("image", u)
            elif ul.endswith(".json"):
                m.setdefault("json", u)
            elif "/ipfs/" in ul or "arweave.net/" in ul or ul.startswith(C.IPFS_GATEWAY.lower()):
                m.setdefault("blob", u)          # image or metadata JSON: decided by fetching it
            else:
                m.setdefault("website", u)
        elif low.startswith("@") and 2 < len(s) < 40 and " " not in s:
            m.setdefault("twitter", "https://x.com/" + s[1:])
        elif " " in s and len(s) >= 12:
            texts.append(s)
    if texts and not m.get("description"):
        m["description"] = max(texts, key=len)
    return m


def merge_json(m: dict, j: dict):
    def pick(*keys):
        for k in keys:
            v = j.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return ""
    for k, keys in (("image", ("image", "imageUrl", "image_url", "logo", "icon", "imageURI")),
                    ("description", ("description", "desc", "about")),
                    ("twitter", ("twitter", "x", "twitterUrl")), ("telegram", ("telegram", "tg", "telegramUrl")),
                    ("website", ("website", "web", "site", "url"))):
        v = pick(*keys)
        if v and not m.get(k):
            m[k] = _url(v) if k in ("image", "website") or v.startswith(("http", "t.me", "x.com")) else v
    links = j.get("links") or j.get("socials")
    if isinstance(links, dict):
        merge_json(m, links)
    for k in ("twitter", "telegram"):
        v = m.get(k, "")
        if v and not v.startswith("http"):
            v = v.lstrip("@")
            m[k] = ("https://x.com/" if k == "twitter" else "https://t.me/") + v


async def resolve(http: httpx.AsyncClient, tx_input: str, logs: list[dict], name: str, symbol: str) -> dict:
    strings = abi_strings(tx_input, True)
    for lg in logs or []:
        strings += [s for s in abi_strings(lg.get("data") or "", False) if s not in strings]
    m = classify(strings, name, symbol)
    for key in ("json", "blob"):
        u = m.pop(key, None)
        if not u:
            continue
        try:
            r = await http.get(u, timeout=10, follow_redirects=True)
            ct = r.headers.get("content-type", "")
            if "json" in ct or r.content[:1] == b"{":
                merge_json(m, r.json())
            elif ct.startswith("image/") and not m.get("image"):
                m["image"] = u
        except Exception as ex:
            log.info("metadata %s: %s", u, ex)
    return m


async def image_bytes(http: httpx.AsyncClient, url: str) -> bytes | None:
    """Picture for send_photo (SVG converted to PNG); None if it can't be loaded."""
    if not url:
        return None
    try:
        r = await http.get(url, timeout=12, follow_redirects=True)
        if r.status_code >= 400 or len(r.content) > MAX_IMG or not r.content:
            return None
        ct = r.headers.get("content-type", "")
        data = r.content
        if "svg" in ct or url.lower().split("?")[0].endswith(".svg") or data.lstrip()[:4] in (b"<svg", b"<?xm"):
            try:
                import cairosvg
                return cairosvg.svg2png(bytestring=data, output_width=512)
            except Exception:
                return None
        if ct.startswith("image/") or data[:4] in (b"\x89PNG", b"GIF8") or data[:3] == b"\xff\xd8\xff" \
                or data[8:12] == b"WEBP":
            return data
    except Exception as ex:
        log.info("image %s: %s", url, ex)
    return None

