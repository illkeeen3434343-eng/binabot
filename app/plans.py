"""Subscription plans. Limits live ONLY here; everything else reads them.

The defaults below can be changed by an admin from Telegram (Admin paneli → Plan
limitləri); the changed values are stored in the database and loaded at start."""
from __future__ import annotations

import json
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class Plan:
    key: str
    title: str
    max_links: int
    ads_per_hour: int
    rank: int


DEFAULTS = {
    "basic": Plan("basic", "Basic", 1, 10, 1),
    "pro": Plan("pro", "Pro", 3, 25, 2),
    "platinum": Plan("platinum", "Platinum", 5, 50, 3),
}
PLANS = dict(DEFAULTS)
LIMIT_RANGE = {"ads_per_hour": (1, 1000), "max_links": (1, 50)}
ORDER = ["basic", "pro", "platinum"]

ST_NEW, ST_PENDING, ST_ACTIVE = "new", "pending", "active"
ST_REJECTED, ST_SUSPENDED, ST_EXPIRED = "rejected", "suspended", "expired"


def get_plan(key: str | None) -> Plan | None:
    return PLANS.get(key or "")


def next_plan(key: str | None) -> Plan | None:
    p = get_plan(key)
    if not p:
        return None
    i = ORDER.index(p.key)
    return PLANS[ORDER[i + 1]] if i + 1 < len(ORDER) else None


def reset_defaults() -> None:
    PLANS.clear()
    PLANS.update(DEFAULTS)


async def load_limits(db) -> None:
    """Apply limits saved by an admin (called once at start)."""
    reset_defaults()
    for key in ORDER:
        raw = await db.kv_get(f"plan:{key}", "")
        if raw:
            d = json.loads(raw)
            PLANS[key] = replace(PLANS[key], max_links=int(d["max_links"]), ads_per_hour=int(d["ads_per_hour"]))


async def set_limit(db, key: str, field: str, value: int) -> None:
    lo, hi = LIMIT_RANGE[field]
    if key not in PLANS or not lo <= value <= hi:
        raise ValueError(field)
    PLANS[key] = replace(PLANS[key], **{field: value})
    await db.kv_set(f"plan:{key}", json.dumps({"max_links": PLANS[key].max_links,
                                               "ads_per_hour": PLANS[key].ads_per_hour}))
