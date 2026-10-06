"""
Small upload endpoint that runs INSIDE the Discord bot, so volunteers' readers and the Photon recorder
can send finished matches straight to the bot's own database. No separate server is needed.

It starts automatically with the bot, but only if an upload key is set. Settings (host's Variables page or .env):
    ZH_INGEST_KEY   a long random text. Senders must send it in an "X-Api-Key" header. Without it the endpoint stays OFF.
    INGEST_PORT     the port to listen on (the host usually shows which port you were given; falls back to PORT / SERVER_PORT)

Test it from any computer:
    curl http://YOUR-HOST:PORT/health
"""
import asyncio
import hmac
import os
import re
import time
from types import SimpleNamespace

from aiohttp import web

from zerohour_db import save_match

MAX_BODY = 256 * 1024
KEY_OK = re.compile(r"^(steam:|epic:)?[A-Za-z0-9]{8,40}$")
STEAM64 = re.compile(r"^(steam:)?(7656\d{13})$")


def _int(v, lo, hi, name):
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        raise ValueError(f"{name} must be a whole number between {lo} and {hi}")
    return v


def parse_match(data: dict) -> SimpleNamespace:
    """Checks one match and returns it in the shape zerohour_db.save_match expects. Raises ValueError if it looks wrong."""
    if not isinstance(data, dict):
        raise ValueError("the upload must be a JSON object")
    key = data.get("match_key")
    if not isinstance(key, str) or not 6 <= len(key) <= 80:
        raise ValueError("match_key must be a text of 6-80 characters")
    players_in = data.get("players")
    if not isinstance(players_in, list) or not 1 <= len(players_in) <= 64:
        raise ValueError("a match needs between 1 and 64 players")
    mode = data.get("mode")
    if mode is not None and (not isinstance(mode, str) or len(mode) > 40):
        raise ValueError("mode must be short text")
    map_ = data.get("map")
    if map_ is not None and (isinstance(map_, bool) or not isinstance(map_, (int, str)) or len(str(map_)) > 60):
        raise ValueError("map must be a number or short text")
    ended = data.get("ended_at")
    if ended is not None:
        if isinstance(ended, bool) or not isinstance(ended, (int, float)) or not 1.5e9 < ended < time.time() + 86400:
            raise ValueError("ended_at must be a normal timestamp")

    players = []
    for p in players_in:
        if not isinstance(p, dict):
            raise ValueError("each player must be an object")
        pk = p.get("player_key")
        if not isinstance(pk, str) or not KEY_OK.match(pk):
            raise ValueError("player_key must be a Steam ID or an 'epic:...' ID")
        m = STEAM64.match(pk)
        if m:
            pk = m.group(2)                      # Steam IDs are stored as the plain 17 digits, as the bot expects
        name = p.get("name")
        if not isinstance(name, str) or not 1 <= len(name) <= 80:
            raise ValueError("name must be text of 1-80 characters")
        team = p.get("team")
        if team is not None and (not isinstance(team, str) or len(team) > 20):
            raise ValueError("team must be short text")
        players.append(SimpleNamespace(
            player_key=pk, name=name, team=team,
            kills=_int(p.get("kills", 0), -100, 500, "kills"),      # kills can go below zero after team kills
            deaths=_int(p.get("deaths", 0), 0, 500, "deaths"),
            assists=_int(p.get("assists", 0), 0, 500, "assists"),
            matchpoints=_int(p.get("matchpoints", 0), 0, 100000, "matchpoints"),
            won=bool(p.get("won", False)),
        ))
    return SimpleNamespace(match_key=key, map=map_, mode=mode, ended_at=ended, players=players)


async def _ingest(request: web.Request) -> web.Response:
    key = os.environ.get("ZH_INGEST_KEY", "")
    sent = request.headers.get("X-Api-Key", "")
    if not key or not hmac.compare_digest(sent.encode(), key.encode()):
        return web.json_response({"error": "wrong or missing upload key"}, status=401)
    try:
        match = parse_match(await request.json())
    except ValueError as err:
        return web.json_response({"error": str(err)}, status=422)
    except Exception:
        return web.json_response({"error": "the upload was not valid JSON"}, status=400)
    stored = await asyncio.to_thread(save_match, match)
    print(f"[ingest] {'stored' if stored else 'already had'} match {match.match_key} ({len(match.players)} players)")
    return web.json_response({"stored": stored})


async def _health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def start_ingest_server():
    """Starts the endpoint (called when the bot starts). Quietly does nothing if no key or port is set."""
    if not os.environ.get("ZH_INGEST_KEY", "").strip():
        print("[ingest] ZH_INGEST_KEY is not set, so the upload endpoint is OFF.")
        return
    port = next((os.environ[k] for k in ("INGEST_PORT", "SERVER_PORT", "PORT") if os.environ.get(k, "").isdigit()), None)
    if not port:
        print("[ingest] No port found (set INGEST_PORT), so the upload endpoint is OFF.")
        return
    app = web.Application(client_max_size=MAX_BODY)
    app.add_routes([web.post("/api/ingest", _ingest), web.get("/health", _health)])
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        await web.TCPSite(runner, "0.0.0.0", int(port)).start()
    except OSError as err:
        print(f"[ingest] Could not listen on port {port}: {err}. The upload endpoint is OFF.")
        return
    print(f"[ingest] Upload endpoint is listening on port {port}.")
