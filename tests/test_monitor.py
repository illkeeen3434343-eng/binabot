import time

from app.db import Database
from tests.conftest import URL_A, URL_B, node

BASE = 6_500_000          # "today's" ID range


def ids(rows):
    return sorted(r["ad_id"] for r in rows)


async def queue(env, uid):
    return ids(await env.db.queued(uid, 100))


async def test_baseline_is_silent_then_only_new_ads(env):
    await env.mkuser(10); await env.mklink(10)
    env.http.feeds['"roomIds"'] = [[node(BASE + 1), node(BASE + 2)]]
    r = await env.mon.run_cycle()
    assert r["ok"] == 1 and await queue(env, 10) == []          # first read: no flood
    env.http.feeds['"roomIds"'] = [[node(BASE + 3), node(BASE + 1), node(BASE + 2)]]
    await env.mon.run_cycle()
    assert await queue(env, 10) == [str(BASE + 3)]
    await env.mon.run_cycle()                                    # same feed again
    assert await queue(env, 10) == [str(BASE + 3)]               # no duplicate


async def test_same_search_is_fetched_once_for_many_users(env):
    for uid in (10, 11, 12):
        await env.mkuser(uid); await env.mklink(uid)
    await env.mklink(11, URL_A.replace("https://", "http://www."))   # same search, other spelling
    env.http.feeds['"roomIds"'] = [[node(BASE + 1)]]
    await env.mon.run_cycle()
    assert len(env.http.calls) == 1                              # baseline: feed has one page
    env.http.calls.clear()
    env.http.feeds['"roomIds"'] = [[node(BASE + 2), node(BASE + 1)]]
    await env.mon.run_cycle()
    assert len(env.http.calls) == 1                              # ONE request for 3 users
    for uid in (10, 11, 12):
        assert await queue(env, uid) == [str(BASE + 2)]


async def test_results_do_not_leak_between_searches(env):
    await env.mkuser(10); await env.mklink(10, URL_A)
    await env.mkuser(20); await env.mklink(20, URL_B)
    env.http.feeds['"roomIds"'] = [[node(BASE + 1)]]
    env.http.feeds['"leased": true'] = [[node(BASE + 50)]]
    await env.mon.run_cycle()
    env.http.feeds['"roomIds"'] = [[node(BASE + 2), node(BASE + 1)]]
    env.http.feeds['"leased": true'] = [[node(BASE + 51), node(BASE + 50)]]
    await env.mon.run_cycle()
    assert await queue(env, 10) == [str(BASE + 2)]
    assert await queue(env, 20) == [str(BASE + 51)]


async def test_old_ad_bumped_to_top_is_not_new(env):
    await env.mkuser(10); await env.mklink(10)
    env.http.feeds['"roomIds"'] = [[node(BASE + 1)]]
    await env.mon.run_cycle()
    old_vip = node(BASE - 40 * 2800, isVipped=True)             # ~40 days old, paid bump
    old_plain = node(BASE - 10 * 2800)                           # ~10 days old, "irəli çək"
    dated_old = node(BASE + 5, createdAt="2026-01-01T00:00:00Z") # API gives the date
    fresh_vip = node(BASE + 9, isVipped=True)                    # new AND promoted -> announce
    env.http.feeds['"roomIds"'] = [[old_vip, fresh_vip, old_plain, dated_old, node(BASE + 1)]]
    await env.mon.run_cycle()
    assert await queue(env, 10) == [str(BASE + 9)]


async def test_paging_continues_only_while_pages_have_unseen_ads(env):
    await env.mkuser(10); await env.mklink(10)
    p1 = [node(BASE + i) for i in range(16)]
    env.http.feeds['"roomIds"'] = [p1, [node(BASE - 1)], [node(BASE - 2)]]
    await env.mon.run_cycle(); env.http.calls.clear()            # seeded pages 1-2
    new = [node(BASE + 100 + i) for i in range(16)]
    env.http.feeds['"roomIds"'] = [new, [node(BASE + 99)] + p1[:15], p1, p1]
    await env.mon.run_cycle()
    assert len(env.http.calls) == 3      # page1 all new -> page2 one new -> page3 nothing -> stop
    assert len(await queue(env, 10)) == 17


async def test_client_side_filter_and_inactive_users(env):
    await env.mkuser(10); await env.mklink(10, URL_A + "&has_repair=true")
    await env.mkuser(11, status="suspended"); await env.mklink(11, URL_A + "&has_repair=true")
    env.http.feeds['"roomIds"'] = [[node(BASE + 1)]]
    await env.mon.run_cycle()
    env.http.feeds['"roomIds"'] = [[node(BASE + 2, hasRepair=False), node(BASE + 3), node(BASE + 1)]]
    await env.mon.run_cycle()
    assert await queue(env, 10) == [str(BASE + 3)]
    assert await queue(env, 11) == []                            # suspended: nothing


async def test_expired_and_over_allowance_links_are_not_monitored(env):
    await env.mkuser(10, plan="basic")
    await env.mklink(10, URL_A); await env.mklink(10, URL_B)     # basic allows 1 (e.g. after downgrade)
    await env.mkuser(11, days=-1); await env.mklink(11, URL_B)   # validity already over
    assert [s["url"] for s, _ in await env.db.monitored()] == ["https://bina.az/baki/alqi-satqi/menziller?room_ids%5B%5D=3"]


async def test_block_stops_cycle_sets_cooldown_and_alerts_once(env):
    await env.mkuser(10); await env.mklink(10, URL_A)
    await env.mkuser(20); await env.mklink(20, URL_B)
    env.http.fail = 403
    r = await env.mon.run_cycle()
    assert r["failed"] == 1 and len(env.http.calls) == 1         # 403 not retried, 2nd search not tried
    assert len(env.admin_msgs) == 1 and "403" in env.admin_msgs[0]
    assert (await env.mon.run_cycle())["skipped"] == "cooldown"  # no knocking during cool-down
    assert len(env.http.calls) == 1
    r = await env.mon.run_cycle(force=True)                      # admin's manual run may override
    assert len(env.admin_msgs) == 1                              # same error: not repeated
    env.http.fail = None
    r = await env.mon.run_cycle(force=True)
    assert r["ok"] == 2 and "yenidən işləyir" in env.admin_msgs[-1]
    for code in (429, 522):
        env.http.fail = code
        await env.db.kv_set("bina_blocked_until", 0)
        await env.mon.run_cycle()
        assert any(str(code) in m for m in env.admin_msgs)


async def test_network_errors_are_retried_a_bounded_number_of_times(env):
    await env.mkuser(10); await env.mklink(10)
    env.http.fail = TimeoutError()
    r = await env.mon.run_cycle()
    assert len(env.http.calls) == 2 and r["note"].startswith("şəbəkə xətası")   # 1 try + 1 retry
    assert "qoşulmaq olmadı" in env.admin_msgs[0]


async def test_schema_change_is_reported_not_crashed(env):
    await env.mkuser(10); await env.mklink(10)
    env.http.feeds['"roomIds"'] = None

    async def bad(url, params=None, headers=None):
        from app.bina.client import Response
        return Response(200, {}, b'{"errors":[{"message":"PersistedQueryNotFound"}]}')
    env.http.get = bad
    r = await env.mon.run_cycle()
    assert r["note"] == "struktur" and "BINA_PERSISTED_HASH" in env.admin_msgs[0]


async def test_history_survives_restart(env):
    await env.mkuser(10); await env.mklink(10)
    env.http.feeds['"roomIds"'] = [[node(BASE + 1)]]
    await env.mon.run_cycle()
    env.http.feeds['"roomIds"'] = [[node(BASE + 2), node(BASE + 1)]]
    await env.mon.run_cycle()
    d = (await env.db.queued(10, 10))[0]
    await env.db.mark_delivery(d["id"], "sent")
    await env.db.close()                                         # "VM restart"
    env.db.c = (await Database(env.cfg.db_path).open()).c
    await env.mon.run_cycle()
    assert await queue(env, 10) == []                            # nothing re-queued
    assert await env.db.sent_since(10, time.time() - 60) == 1


async def test_long_gap_rebaselines_silently(env):
    await env.mkuser(10); await env.mklink(10)
    env.http.feeds['"roomIds"'] = [[node(BASE + 1)]]
    await env.mon.run_cycle()
    await env.db._x("UPDATE searches SET last_ok_at=?", (int(time.time()) - 3 * 86400,))
    env.http.feeds['"roomIds"'] = [[node(BASE + i) for i in range(2, 12)]]
    await env.mon.run_cycle()
    assert await queue(env, 10) == []
