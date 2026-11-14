import json
import time

import pytest

from app.bina.client import BinaClient, Response
from app.bina.links import LinkMap, parse_link
from app.config import ROOT, Config
from app.db import Database
from app.services.alerts import Alerts
from app.services.monitor import Monitor

LMAP = LinkMap.load(str(ROOT / "bina_map.json"))
URL_A = "https://bina.az/baki/alqi-satqi/menziller?room_ids[]=3"
URL_B = "https://bina.az/baki/kiraye/menziller?price_to=900"


def node(i, **kw):
    """A node shaped like the SearchItems answer the existing monitor.py parses."""
    n = {"id": str(i), "path": f"/items/{i}", "rooms": 3, "floor": 8, "floors": 16,
         "area": {"value": 85, "units": "m²"}, "price": {"total": 185000, "currency": "azn"},
         "location": {"id": "51", "name": "Nizami r.", "fullName": "Nizami rayonu"},
         "city": {"name": "Bakı"}, "preview": {"f460x345": f"https://bina.azstatic.com/{i}.jpg"},
         "hasRepair": True, "hasBillOfSale": True, "isFeatured": False, "isVipped": False,
         "updatedAt": "2026-11-14T10:00:00+04:00"}
    n.update(kw)
    return n


def payload(nodes, next_cursor=None):
    return {"data": {"itemsConnection": {
        "totalCount": 999, "edges": [{"node": n} for n in nodes],
        "pageInfo": {"hasNextPage": bool(next_cursor), "endCursor": next_cursor}}}}


class FakeHttp:
    """Scripted Bina.az. `feeds[key]` = list of pages (lists of nodes), where key is a
    substring of the filter JSON; `fail` = status code or Exception for every request."""

    def __init__(self):
        self.feeds, self.fail, self.calls, self.images, self.gone = {}, None, [], True, set()

    async def get(self, url, params=None, headers=None):
        self.calls.append((url, params))
        if self.fail is not None:
            if isinstance(self.fail, Exception):
                raise self.fail
            return Response(self.fail, {}, b"blocked")
        if "graphql" in url:
            v = json.loads(params["variables"])
            key = json.dumps(v["filter"], sort_keys=True)
            pages = next((p for k, p in self.feeds.items() if k in key), [[]])
            i = int(v.get("cursor", "0"))
            nxt = str(i + 1) if i + 1 < len(pages) else None
            return Response(200, {}, json.dumps(payload(pages[i], nxt)).encode())
        if url.endswith(".jpg"):
            return Response(200 if self.images else 404, {}, b"\xff\xd8" + b"0" * 2000)
        return Response(404 if url in self.gone else 200, {}, b"<html>" + b"x" * 2000)


class Sink:
    def __init__(self):
        self.msgs = []

    async def __call__(self, text):
        self.msgs.append(text)


@pytest.fixture
async def env(tmp_path):
    cfg = Config(db_path=str(tmp_path / "t.sqlite3"), admin_ids=frozenset({1}),
                 request_min_interval=0, send_delay=0, payment_info="Kart: 0000")
    db = await Database(cfg.db_path).open()
    http = FakeHttp()

    async def nosleep(_):
        return None
    client = BinaClient(http, "hash", min_interval=0, retries=1, sleep=nosleep)
    sink = Sink()
    alerts = Alerts(db, cfg, sink)
    mon = Monitor(db, cfg, client, alerts)

    class E:
        pass
    e = E()
    e.cfg, e.db, e.http, e.client, e.alerts, e.mon, e.admin_msgs, e.lmap = (
        cfg, db, http, client, alerts, mon, sink.msgs, LMAP)

    async def mkuser(uid, plan="basic", status="active", days=30):
        await db.upsert_user(uid, f"u{uid}", f"User{uid}")
        if status == "active":
            await db.activate(uid, plan, int(time.time()) + days * 86400)
        else:
            await db.set_user(uid, status=status, plan=plan)
        return uid

    async def mklink(uid, url=URL_A):
        return await db.add_link(uid, parse_link(url, LMAP))
    e.mkuser, e.mklink = mkuser, mklink
    yield e
    await db.close()
