"""Controlled check of ONE Bina.az link from this machine (run it on the VM):

    python -m app.tools.probe "https://bina.az/baki/alqi-satqi/menziller?room_ids[]=3"

Makes exactly one request. Shows how the link is normalised and translated, the
total Bina.az reports (compare it with the number on the website for the same
search!) and the first ads. Use it after editing bina_map.json or when the admin
gets a "schema" error (persisted query hash changed).
"""
import asyncio
import json
import sys

from ..bina.client import API_HEADERS, GRAPHQL_URL, BinaClient, Http
from ..bina.links import LinkError, LinkMap, parse_link
from ..bina.parser import parse_search_payload
from ..config import load_config


async def main(url: str):
    cfg = load_config()
    try:
        p = parse_link(url, LinkMap.load(cfg.map_path), cfg.unknown_params)
    except LinkError as e:
        sys.exit(f"REJECTED ({e.kind}): {e.detail}")
    print("normalised :", p.url)
    print("filter     :", json.dumps(p.filter, ensure_ascii=False))
    print("local check:", p.client_filter or "-")
    http = Http(cfg.request_timeout)
    client = BinaClient(http, cfg.persisted_hash, 0, 0)
    try:
        r = await client._get(GRAPHQL_URL, params=client._params(p.filter, None), headers=API_HEADERS)
        payload = r.json()
        ads, cursor, total = parse_search_payload(payload)
        edges = payload["data"]["itemsConnection"]["edges"]
        print("HTTP       :", r.status, "| total on Bina.az:", total, "| next page:", bool(cursor))
        if edges:
            print("node fields:", ", ".join(sorted(edges[0]["node"].keys())))
        sub = {v: k for k, v in LinkMap.load(cfg.map_path).subcategories.items()}
        if p.filter.get("categoryId") in sub:
            # self-check of the sub-category ids: new-build + old-build must add up to all apartments
            totals = {}
            for cid in ("1", "2", "3"):
                rr = await client._get(GRAPHQL_URL, params=client._params({**p.filter, "categoryId": cid}, None),
                                       headers=API_HEADERS)
                totals[cid] = parse_search_payload(rr.json())[2]
                await asyncio.sleep(2)
            ok = None not in totals.values() and totals["2"] + totals["3"] == totals["1"] and totals["2"] != totals["1"]
            print(f"category check: menziller={totals['1']}  yeni-tikili={totals['2']}  kohne-tikili={totals['3']}  ->",
                  "OK (2 + 3 = 1)" if ok else "MISMATCH - sub-category ids in bina_map.json are probably wrong")
        for a in ads[:8]:
            print(f"  {a.id}  {a.price} {a.currency}  {a.rooms} otaq  {a.area} {a.area_units}  "
                  f"{a.floor}/{a.floors}  {a.location}  promoted={a.promoted}  photo={'yes' if a.photo else 'no'}")
    except Exception as e:
        sys.exit(f"FAILED: {e.__class__.__name__}: {e}")
    finally:
        await http.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    asyncio.run(main(sys.argv[1]))
