"""
Small upload endpoint that runs INSIDE the Discord bot, so volunteers' readers and the Photon recorder
can send finished matches straight to the bot's own database. No separate server is needed.

It starts automatically with the bot, but only if an upload key is set. Settings (host's Variables page or .env):
    ZH_INGEST_KEY   a long random text. Senders must send it in an "X-Api-Key" header. Without it the endpoint stays OFF.
    INGEST_PORT     the port to listen on (the host usually shows which port you were given; falls back to PORT / SERVER_PORT)

Every accepted upload is also kept as a plain file in a folder, as a backup and a record of the original data:
    received/accepted/<date>/<source>_<match>.json     (what was uploaded, untouched)
    received/rejected/<date>/...                        (uploads with the right key but bad data, to help debugging)
    ZH_ARCHIVE_DIR      change the folder name/location (default: received)
    ZH_ARCHIVE_DAYS     delete files older than this many days (default: 90)
    ZH_ARCHIVE_MAX_MB   stop saving when the folder is bigger than this (default: 200)

Test it from any computer:
    curl http://YOUR-HOST:PORT/health
"""
import asyncio
import hmac
import os
import json
import re
import shutil
import time

from aiohttp import web

from zerohour_db import save_match
from zhsb_parse import parse_match

MAX_BODY = 256 * 1024


# What /ingest shows in Discord (so you can check the endpoint without the host's console).
STATUS = {"listening": False, "port": None, "message": "not started yet", "received": 0, "stored": 0, "rejected": 0, "last_at": None}

ARCHIVE_DIR = os.environ.get("ZH_ARCHIVE_DIR", "received")
ARCHIVE_DAYS = int(os.environ.get("ZH_ARCHIVE_DAYS", "90") or 90)
ARCHIVE_MAX_MB = int(os.environ.get("ZH_ARCHIVE_MAX_MB", "200") or 200)
_SAFE = re.compile(r"[^A-Za-z0-9._-]")


def _folder_mb(path: str) -> float:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total / 1048576


def archive(data, match_key, stored, folder="accepted", reason=None):
    """Keeps a copy of an upload exactly as it arrived. Never raises: a problem here must not lose the match."""
    try:
        if _folder_mb(ARCHIVE_DIR) > ARCHIVE_MAX_MB:
            print(f"[ingest] The {ARCHIVE_DIR} folder is over {ARCHIVE_MAX_MB} MB, so this upload was not archived.")
            return
        source = _SAFE.sub("_", str(data.get("source", "unknown")))[:30] if isinstance(data, dict) else "unknown"
        day_dir = os.path.join(ARCHIVE_DIR, folder, time.strftime("%Y-%m-%d", time.gmtime()))
        os.makedirs(day_dir, exist_ok=True)
        path = os.path.join(day_dir, f"{source}_{_SAFE.sub('_', str(match_key))[:80]}.json")
        if folder == "accepted" and os.path.exists(path):
            return   # the same match sent again: keep the first copy
        envelope = {"received_at": time.time(), "stored_in_database": stored, "reason": reason, "data": data}
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(envelope, f, ensure_ascii=False, indent=1)
        os.replace(path + ".tmp", path)
    except Exception as err:
        print(f"[ingest] Could not archive an upload: {err}")


def purge_old():
    """Deletes archive day-folders older than ZH_ARCHIVE_DAYS."""
    cutoff = time.time() - ARCHIVE_DAYS * 86400
    for folder in ("accepted", "rejected"):
        base = os.path.join(ARCHIVE_DIR, folder)
        if not os.path.isdir(base):
            continue
        for day in os.listdir(base):
            try:
                if time.mktime(time.strptime(day, "%Y-%m-%d")) < cutoff:
                    shutil.rmtree(os.path.join(base, day))
                    print(f"[ingest] Removed archive folder {folder}/{day} (older than {ARCHIVE_DAYS} days).")
            except (ValueError, OSError):
                pass


async def _purge_loop():
    while True:
        await asyncio.to_thread(purge_old)
        await asyncio.sleep(86400)


async def _ingest(request: web.Request) -> web.Response:
    key = os.environ.get("ZH_INGEST_KEY", "")
    sent = request.headers.get("X-Api-Key", "")
    if not key or not hmac.compare_digest(sent.encode(), key.encode()):
        return web.json_response({"error": "wrong or missing upload key"}, status=401)
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "the upload was not valid JSON"}, status=400)
    try:
        match = parse_match(data)
    except ValueError as err:
        STATUS["rejected"] += 1
        await asyncio.to_thread(archive, data, data.get("match_key", "unknown") if isinstance(data, dict) else "unknown", False, "rejected", str(err))
        return web.json_response({"error": str(err)}, status=422)
    stored = await asyncio.to_thread(save_match, match)
    STATUS["received"] += 1; STATUS["stored"] += 1 if stored else 0; STATUS["last_at"] = time.time()
    await asyncio.to_thread(archive, data, match.match_key, stored)
    print(f"[ingest] {'stored' if stored else 'already had'} match {match.match_key} ({len(match.players)} players)")
    return web.json_response({"stored": stored})


async def _health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def start_ingest_server():
    """Starts the endpoint (called when the bot starts). Quietly does nothing if no key or port is set."""
    if not os.environ.get("ZH_INGEST_KEY", "").strip():
        STATUS["message"] = "OFF: the variable ZH_INGEST_KEY is not set"
        print("[ingest] ZH_INGEST_KEY is not set, so the upload endpoint is OFF.")
        return
    port = next((os.environ[k] for k in ("INGEST_PORT", "SERVER_PORT", "PORT") if os.environ.get(k, "").isdigit()), None)
    if not port:
        STATUS["message"] = "OFF: no port found (add a variable INGEST_PORT with your port number)"
        print("[ingest] No port found (set INGEST_PORT), so the upload endpoint is OFF.")
        return
    app = web.Application(client_max_size=MAX_BODY)
    app.add_routes([web.post("/api/ingest", _ingest), web.get("/health", _health)])
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        await web.TCPSite(runner, "0.0.0.0", int(port)).start()
    except OSError as err:
        STATUS["message"] = f"OFF: could not listen on port {port} ({err})"
        print(f"[ingest] Could not listen on port {port}: {err}. The upload endpoint is OFF.")
        return
    asyncio.create_task(_purge_loop())
    STATUS.update(listening=True, port=int(port), message=f"ON: listening on port {port}")
    print(f"[ingest] Upload endpoint is listening on port {port}. Archiving uploads to the '{ARCHIVE_DIR}' folder.")
