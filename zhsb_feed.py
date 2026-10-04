"""
Reads the live server list from zhsb.info (used with the site owner's permission).

Test it on its own (prints what it found):
    python zhsb_feed.py

How it works: when someone uses a command, the bot connects to the site's live
feed, waits for one full update, then disconnects. The answer is reused for 30
seconds, so the site is contacted at most about twice a minute however many
people use the commands. Nothing needs installing: aiohttp comes with discord.py.
"""
import asyncio
import json
import time

import aiohttp

# The address of the site's live feed (a WebSocket, starting with wss://).
# If this is wrong, find the right one in your browser: F12 > Network > WS,
# click the entry, and copy "Request URL" from the Headers tab.
WS_URL = "wss://api.zhsb.info/ws"

# Browsers tell a site which page they are connecting from, and many sites refuse
# connections without it (this causes a "403" error). The bot sends the same value
# the site's own page does. Ask the site owner to confirm this is how they want
# the bot to connect.
ORIGIN = "https://zhsb.info"

CACHE_SECONDS = 30
WAIT_SECONDS = 10   # how long to wait for the site to send its list
USER_AGENT = "ZeroHourStatBot/1.0 (community Discord bot; permission from site owner)"

_lock = asyncio.Lock()
_cache = {"at": 0.0, "data": None}


def dig(data, *path, default="?"):
    """Safely read a nested value, e.g. dig(room, "map", "name"). Returns default if missing."""
    for key in path:
        if not isinstance(data, dict) or key not in data:
            return default
        data = data[key]
    return data


async def _fetch() -> dict:
    async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as session:
        async with session.ws_connect(WS_URL, origin=ORIGIN) as ws:
            deadline = time.monotonic() + WAIT_SECONDS
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("zhsb.info did not send a server list in time")
                msg = await ws.receive(timeout=remaining)
                if msg.type in (aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY):
                    try:
                        data = json.loads(msg.data)
                    except ValueError:
                        continue  # not JSON, ignore it
                    if isinstance(data, dict) and "rooms" in data and "totals" in data:
                        return data
                elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING,
                                  aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    raise ConnectionError("zhsb.info closed the connection")


async def get_zhsb():
    """Returns (data, fetched_at). Uses the cache when it is fresh; falls back to old data if the site is down."""
    async with _lock:
        if _cache["data"] is not None and time.time() - _cache["at"] < CACHE_SECONDS:
            return _cache["data"], _cache["at"]
        try:
            data = await asyncio.wait_for(_fetch(), timeout=WAIT_SECONDS + 5)
        except Exception:
            if _cache["data"] is not None and time.time() - _cache["at"] < 300:
                return _cache["data"], _cache["at"]  # up to 5 minutes old is better than nothing
            raise
        _cache.update(at=time.time(), data=data)
        return data, _cache["at"]


if __name__ == "__main__":
    async def _test():
        data, _ = await get_zhsb()
        print("Connected. Public:", dig(data, "totals", "public"), "| All:", dig(data, "totals", "all"))
        for r in data.get("rooms", []):
            print(f"  {dig(r, 'region', 'name')} | {dig(r, 'match', 'id')} | {dig(r, 'players', 'summary')} | "
                  f"{dig(r, 'map', 'name')} | {dig(r, 'match', 'summary')}")
    asyncio.run(_test())
