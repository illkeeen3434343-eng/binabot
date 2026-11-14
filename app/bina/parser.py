"""Bina.az response -> Ad objects. The ONLY place that knows Bina.az field names.

If Bina.az changes its API, this file (and client.py's request shape) is what to
update; the monitoring engine only sees Ad objects.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import asdict, dataclass

PROMO_FIELDS = ("isFeatured", "isVipped")          # confirmed in the existing project
_PROMO_KEY = re.compile(r"vip|premium|featured|promoted|boost|highlight|paid|sticky|urgent", re.I)


class SchemaError(Exception):
    """The answer does not look like what we expect (API changed / query expired)."""


@dataclass
class Ad:
    id: str
    url: str
    price: int | None = None
    currency: str = "AZN"
    rooms: int | None = None
    area: float | None = None
    area_units: str = "m²"
    floor: int | None = None
    floors: int | None = None
    location: str | None = None
    photo: str | None = None
    promoted: bool = False
    has_repair: bool | None = None
    created_ts: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Ad":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


def _sub(node, key, fld):
    o = node.get(key)
    return o.get(fld) if isinstance(o, dict) else None


def _to_int(v):
    try:
        return int(float(v)) if v is not None else None
    except (TypeError, ValueError):
        return None


def _to_float(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _to_ts(v):
    if isinstance(v, (int, float)) and v > 0:
        return float(v / 1000 if v > 1e11 else v)
    if isinstance(v, str) and v[:4].isdigit():
        try:
            d = dt.datetime.fromisoformat(v.replace("Z", "+00:00"))
            return (d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)).timestamp()
        except ValueError:
            return None
    return None


def parse_node(node: dict) -> Ad:
    ad_id = str(node["id"])
    preview = node.get("preview") if isinstance(node.get("preview"), dict) else {}
    photo = preview.get("f460x345") or preview.get("large") or preview.get("thumbnail")
    promoted = any(node.get(k) is True for k in PROMO_FIELDS) or any(
        v is True and _PROMO_KEY.search(k) for k, v in node.items())
    path = node.get("path")
    return Ad(
        id=ad_id,
        url=f"https://bina.az{path}" if isinstance(path, str) and path.startswith("/")
        else f"https://bina.az/items/{ad_id}",
        price=_to_int(_sub(node, "price", "total")),
        currency=_sub(node, "price", "currency") or "AZN",
        rooms=_to_int(node.get("rooms")),
        area=_to_float(_sub(node, "area", "value")),
        area_units=_sub(node, "area", "units") or "m²",
        floor=_to_int(node.get("floor")),
        floors=_to_int(node.get("floors")),
        location=_sub(node, "location", "fullName") or _sub(node, "location", "name")
        or _sub(node, "city", "name"),
        photo=photo if isinstance(photo, str) and photo.startswith("http") else None,
        promoted=bool(promoted),
        has_repair=node.get("hasRepair") if isinstance(node.get("hasRepair"), bool) else None,
        created_ts=_to_ts(node.get("createdAt") or node.get("publishedAt")),
    )


def parse_search_payload(payload) -> tuple[list[Ad], str | None, int | None]:
    """-> (ads on this page, cursor of the next page or None, total count or None)."""
    if not isinstance(payload, dict):
        raise SchemaError("answer is not a JSON object")
    if payload.get("errors"):
        msg = "; ".join(str(e.get("message", e)) if isinstance(e, dict) else str(e)
                        for e in payload["errors"])
        if "persistedquery" in msg.lower().replace("_", "").replace(" ", ""):
            raise SchemaError("persisted query expired - update BINA_PERSISTED_HASH: " + msg[:150])
        raise SchemaError("GraphQL error: " + msg[:200])
    conn = (payload.get("data") or {}).get("itemsConnection")
    if not isinstance(conn, dict) or not isinstance(conn.get("edges"), list):
        raise SchemaError("no itemsConnection.edges in the answer")
    ads = []
    for edge in conn["edges"]:
        node = edge.get("node") if isinstance(edge, dict) else None
        if isinstance(node, dict) and node.get("id") is not None:
            ads.append(parse_node(node))
    info = conn.get("pageInfo") or {}
    cursor = info.get("endCursor") if info.get("hasNextPage") else None
    return ads, cursor or None, conn.get("totalCount")
