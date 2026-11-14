"""Validate and normalise a Bina.az search link, and translate it to a search filter.

Independent of the network layer. The URL -> filter vocabulary is data
(bina_map.json), so a new city / category / filter is added without code changes.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, quote, urlsplit

ALLOWED_HOSTS = {"bina.az", "www.bina.az", "ru.bina.az", "m.bina.az"}
MAX_URL_LEN = 2000


class LinkError(Exception):
    """kind: 'invalid' (not a Bina.az link / malformed) or 'unsupported'
    (a Bina.az link the bot cannot monitor faithfully)."""

    def __init__(self, kind: str, detail: str = ""):
        super().__init__(f"{kind}: {detail}")
        self.kind, self.detail = kind, detail


@dataclass(frozen=True)
class ParsedLink:
    url: str                                   # canonical form, identical for equal searches
    filter: dict                               # sent to Bina.az
    client_filter: dict = field(default_factory=dict)   # applied to results locally
    ignored: tuple = ()                        # unknown params dropped (UNKNOWN_PARAMS=ignore)


class LinkMap:
    def __init__(self, data: dict):
        self.cities = data.get("cities", {})
        self.deals = data.get("deals", {})
        self.categories = data.get("categories", {})
        self.subcategories = data.get("subcategories", {})     # "category/sub" -> categoryId
        self.list_params = data.get("list_params", {})
        self.number_params = data.get("number_params", {})
        self.bool_params = data.get("bool_params", {})
        self.client_bool_params = data.get("client_bool_params", {})
        self.ignored = set(data.get("ignored_params", []))

    @classmethod
    def load(cls, path: str) -> "LinkMap":
        with open(path, encoding="utf-8") as fh:
            return cls(json.load(fh))


def _num(v: str):
    try:
        f = float(v.replace(" ", "").replace(",", "."))
    except ValueError:
        return None
    if f != f or f in (float("inf"), float("-inf")) or f < 0:
        return None
    return int(f) if f.is_integer() else f


def _bool(v: str):
    v = v.strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    return None


def parse_link(raw: str, lmap: LinkMap, unknown: str = "reject") -> ParsedLink:
    raw = (raw or "").strip()
    if not raw or len(raw) > MAX_URL_LEN or re.search(r"\s", raw):
        raise LinkError("invalid", "empty, too long or contains spaces")
    if "://" not in raw:
        raw = "https://" + raw
    try:
        u = urlsplit(raw)
        host, port = (u.hostname or "").lower().rstrip("."), u.port
    except ValueError:
        raise LinkError("invalid", "malformed")
    if u.scheme.lower() not in ("http", "https"):
        raise LinkError("invalid", "scheme")
    if u.username or u.password or port not in (None, 80, 443):
        raise LinkError("invalid", "userinfo/port")
    if host not in ALLOWED_HOSTS:              # exact match: bina.az.example.com fails here
        raise LinkError("invalid", f"host {host!r}")

    segs = [s for s in u.path.lower().split("/") if s]
    f: dict = {}
    path = []
    if segs and segs[0] in lmap.cities:
        f["cityId"] = lmap.cities[segs[0]]
        path.append(segs.pop(0))
    if not segs or segs[0] not in lmap.deals:
        raise LinkError("unsupported", "path is not a known search section")
    f["leased"] = bool(lmap.deals[segs[0]])
    path.append(segs.pop(0))
    if segs:
        if segs[0] not in lmap.categories:
            raise LinkError("unsupported", f"category {segs[0]!r}")
        f["categoryId"] = lmap.categories[segs[0]]
        path.append(segs.pop(0))
        if segs and f"{path[-1]}/{segs[0]}" in lmap.subcategories:
            f["categoryId"] = lmap.subcategories[f"{path[-1]}/{segs[0]}"]
            path.append(segs.pop(0))
    if segs:
        raise LinkError("unsupported", f"extra path {'/'.join(segs)!r}")

    canon: list[tuple[str, str]] = []
    client: dict = {}
    ignored: list[str] = []
    lists: dict[str, set] = {}
    for k, v in parse_qsl(u.query, keep_blank_values=False):
        k = k.strip()
        if k in lmap.list_params:
            if not re.fullmatch(r"\d{1,9}", v.strip()):
                raise LinkError("unsupported", f"{k}={v}")
            lists.setdefault(k, set()).add(v.strip())
        elif k in lmap.number_params:
            n = _num(v)
            if n is None:
                raise LinkError("unsupported", f"{k}={v}")
            f[lmap.number_params[k]] = n
            canon.append((k, str(n)))
        elif k in lmap.bool_params or k in lmap.client_bool_params:
            b = _bool(v)
            if b is None:
                raise LinkError("unsupported", f"{k}={v}")
            if k in lmap.bool_params:
                f[lmap.bool_params[k]] = b
            else:
                client[lmap.client_bool_params[k]] = b
            canon.append((k, "true" if b else "false"))
        elif k in lmap.ignored or k.startswith("utm_"):
            continue
        elif unknown == "ignore":
            ignored.append(k)
        else:
            raise LinkError("unsupported", f"filter {k!r}")
    for k, vals in lists.items():
        ordered = sorted(vals, key=int)
        f[lmap.list_params[k]] = ordered
        canon.extend((k, v) for v in ordered)

    canon = sorted(set(canon), key=lambda kv: (kv[0], kv[1].zfill(12)))
    url = "https://bina.az/" + "/".join(path)
    if canon:
        url += "?" + "&".join(f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in canon)
    return ParsedLink(url=url, filter=f, client_filter=client, ignored=tuple(sorted(set(ignored))))
