"""Subscription plans. Limits live ONLY here; everything else reads them."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Plan:
    key: str
    title: str
    max_links: int
    ads_per_hour: int
    rank: int


PLANS = {
    "basic": Plan("basic", "Basic", 1, 10, 1),
    "pro": Plan("pro", "Pro", 3, 25, 2),
    "platinum": Plan("platinum", "Platinum", 5, 50, 3),
}
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
