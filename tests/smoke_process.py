"""Manual smoke test of the REAL process (python run.py) against a fake Telegram API.

    python tests/smoke_process.py

Starts a local HTTP server that speaks enough of the Bot API, launches run.py in a
subprocess with a temporary database, delivers a /start update, and checks that the
bot registered the user, answered in Azerbaijani, and reported the (expected, in a
sandbox without internet) Bina.az failure to the admin.
"""
import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time

from aiohttp import web

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
calls, served = [], {"done": False}


async def api(request: web.Request):
    method = request.match_info["method"]
    body = await request.post() if request.can_read_body else {}
    calls.append((method, dict(body)))
    if method == "getMe":
        res = {"id": 42, "is_bot": True, "first_name": "bot", "username": "test_bot"}
    elif method == "getUpdates":
        if not served["done"]:
            served["done"] = True
            res = [{"update_id": 1, "message": {
                "message_id": 1, "date": int(time.time()), "chat": {"id": 777, "type": "private"},
                "from": {"id": 777, "is_bot": False, "first_name": "Ilkin", "username": "tester"},
                "text": "/start", "entities": [{"type": "bot_command", "offset": 0, "length": 6}]}}]
        else:
            await asyncio.sleep(1)
            res = []
    elif method in ("sendMessage", "sendPhoto"):
        res = {"message_id": len(calls), "date": int(time.time()), "chat": {"id": int(body["chat_id"]), "type": "private"},
               "text": body.get("text", "")}
    else:
        res = True
    return web.json_response({"ok": True, "result": res})


async def main():
    app = web.Application()
    app.router.add_route("*", "/bot{token}/{method}", api)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    tmp = tempfile.mkdtemp()
    dbp = os.path.join(tmp, "smoke.sqlite3")

    # pre-seed: one active user with one link, so the monitor really calls Bina.az
    from app.bina.links import LinkMap, parse_link
    from app.db import Database
    db = await Database(dbp).open()
    await db.upsert_user(555, "seed", "Seed")
    await db.activate(555, "basic", int(time.time()) + 86400)
    await db.add_link(555, parse_link("https://bina.az/baki/alqi-satqi/menziller", LinkMap.load(os.path.join(ROOT, "bina_map.json"))))
    await db.close()

    env = dict(os.environ, BOT_TOKEN="42:SMOKE", ADMIN_IDS="999", DB_PATH=dbp, LOG_DIR=os.path.join(tmp, "logs"),
               TELEGRAM_API_BASE=f"http://127.0.0.1:{port}", CHECK_FIRST_DELAY_SEC="1", REQUEST_RETRIES="0",
               REQUEST_TIMEOUT_SEC="8", REQUEST_MIN_INTERVAL_SEC="0")
    proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "run.py")], env=env, cwd=ROOT,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        for _ in range(40):
            await asyncio.sleep(0.5)
            texts = [c[1].get("text", "") for c in calls if c[0] == "sendMessage"]
            if any("Ana səhifə" in t for t in texts) and any("⚠️ <b>Xəta</b>" in t for t in texts):
                break
    finally:
        proc.terminate()
        out = proc.communicate(timeout=15)[0]
        await runner.cleanup()

    sent = [(c[1].get("chat_id"), c[1].get("text", "")) for c in calls if c[0] == "sendMessage"]
    row = sqlite3.connect(dbp).execute("SELECT username, first_name, status FROM users WHERE user_id=777").fetchone()
    errs = sqlite3.connect(dbp).execute("SELECT source, code FROM errors").fetchall()
    cyc = sqlite3.connect(dbp).execute("SELECT trigger, searches_failed, note FROM cycles").fetchall()
    print("methods called :", sorted({c[0] for c in calls}))
    print("user 777 row   :", row)
    print("welcome sent   :", any(cid == "777" and "Necə başlamaq olar" in t for cid, t in sent))
    print("menu sent      :", any(cid == "777" and "Ana səhifə" in t for cid, t in sent))
    print("admin alert    :", [t.replace("\n", " | ")[:110] for cid, t in sent if cid == "999"])
    print("errors table   :", errs, "| cycles:", cyc)
    print("clean shutdown :", "bot stopped" in out, "| exit code:", proc.returncode)
    ok = (row == ("tester", "Ilkin", "new") and any(cid == "999" and "⚠️ <b>Xəta</b>" in t for cid, t in sent)
          and errs and "bot stopped" in out)
    print("\nSMOKE TEST", "PASSED" if ok else "FAILED")
    if not ok:
        print(out[-3000:])
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    asyncio.run(main())
