"""
Reads a player's lifetime Zero Hour stats (kills, deaths, matches won/lost, damage) from Steam.

Needs a Steam Web API key in a setting named STEAM_API_KEY (an environment variable on the host,
or a line STEAM_API_KEY=your-key in the .env file next to start.py). Never put the key in this file.

Test it on its own (prints one player's stats):
    python steam_stats.py 76561199388321185

Notes:
  * Steam only returns stats for players whose "Game details" privacy setting is Public.
  * Results are reused for 10 minutes.
"""
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import steam_leaderboard

APP_ID = steam_leaderboard.APP_ID
CACHE_SECONDS = 600

# Which Steam stat holds a player's total matchpoints. Not known yet (the candidates are
# "zh_MP4", "zh_MP6" and "zh_MP7"), so it is left off. Once you know which one matches the Steam
# leaderboard total, put its name here, e.g. MATCHPOINT_STAT = "zh_MP7".
MATCHPOINT_STAT = None


class SteamStatsError(Exception):
    """Base class. str(error) is a message that is fine to show to a Discord user."""


class NotConfigured(SteamStatsError):
    pass


class BadKey(SteamStatsError):
    pass


class PlayerNotFound(SteamStatsError):
    pass


class PrivateProfile(SteamStatsError):
    pass


def _api(path: str, **params) -> dict:
    key = os.environ.get("STEAM_API_KEY", "").strip()
    if not key:
        raise NotConfigured("Steam stats aren't set up yet (no Steam API key).")
    url = f"https://api.steampowered.com/{path}?" + urllib.parse.urlencode({**params, "key": key})
    req = urllib.request.Request(url, headers={"User-Agent": "ZeroHourStatTracker/1.0 (community stats bot)"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            body = r.read().decode("utf-8", errors="replace").strip()
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise BadKey("Steam rejected the bot's API key.")
        if e.code in (400, 500):
            raise PrivateProfile("No stats are available for that player.")
        raise SteamStatsError("I couldn't reach Steam right now. Please try again in a few minutes.")
    except Exception:
        raise SteamStatsError("I couldn't reach Steam right now. Please try again in a few minutes.")
    return json.loads(body) if body else {}


def _resolve_vanity(name: str) -> str:
    """Turns a custom profile name (steamcommunity.com/id/NAME) into the 17-digit Steam ID."""
    resp = _api("ISteamUser/ResolveVanityURL/v1/", vanityurl=name).get("response", {})
    if resp.get("success") == 1 and resp.get("steamid"):
        return str(resp["steamid"])
    raise PlayerNotFound("I couldn't find that player on Steam.")


def find_steam_id(query: str, local_key=None) -> str:
    """Works out a 17-digit Steam ID from what the user typed: an ID, a profile link, or a name."""
    q = query.strip()
    m = re.search(r"steamcommunity\.com/profiles/(\d{17})", q) or re.fullmatch(r"(\d{17})", q)
    if m:
        return m.group(1)
    m = re.search(r"steamcommunity\.com/id/([^/?\s]+)", q)
    if m:
        return _resolve_vanity(m.group(1))

    if local_key and re.fullmatch(r"7656119\d{10}", str(local_key)):  # the tracker already knows this player
        return str(local_key)

    try:  # look the name up on the Steam top 200
        entries, _ = steam_leaderboard.get_leaderboard()
    except Exception:
        entries = []
    hits = steam_leaderboard.search(entries, q)
    if len(hits) == 1:
        ident = hits[0]["profile"]
        return ident if re.fullmatch(r"\d{17}", ident) else _resolve_vanity(ident)
    if len(hits) > 1:
        raise PlayerNotFound(
            f"More than one top-200 player matches \"{q}\". Use their Steam ID or profile link instead."
        )

    try:  # last try: treat it as a custom profile name
        return _resolve_vanity(q)
    except PlayerNotFound:
        raise PlayerNotFound(
            f"I couldn't find \"{q}\". Steam can't search by name outside the top 200, "
            "so use the player's Steam ID or profile link."
        )


_lock = threading.Lock()
_cache = {}  # steam_id -> {"at": time, "data": {...}}


def get_user_stats(steam_id: str) -> dict:
    """Lifetime stats for one player. Raises PrivateProfile if Steam has nothing to show."""
    with _lock:
        cached = _cache.get(steam_id)
        if cached and time.time() - cached["at"] < CACHE_SECONDS:
            return cached["data"]

    data = _api("ISteamUserStats/GetUserStatsForGame/v2/", steamid=steam_id, appid=APP_ID)
    raw_stats = (data.get("playerstats") or {}).get("stats")
    if not raw_stats:
        raise PrivateProfile("No stats are available for that player.")
    values = {s["name"]: s.get("value", 0) for s in raw_stats}  # stats Steam doesn't list count as 0

    kills = values.get("zh_Kills", 0)
    deaths = values.get("zh_Deaths", 0)
    wins = values.get("zh_MatchesWon", 0)
    losses = values.get("zh_MatchesLost", 0)
    matches = wins + losses
    result = {
        "steam_id": steam_id,
        "kills": kills,
        "deaths": deaths,
        "kd": round(kills / deaths, 2) if deaths else float(kills),
        "wins": wins,
        "losses": losses,
        "matches": matches,
        "win_rate": round(100 * wins / matches, 1) if matches else 0.0,
        "damage": values.get("zh_DamagesDone", 0),
        "matchpoints": values.get(MATCHPOINT_STAT) if MATCHPOINT_STAT else None,
        "raw": values,
    }
    with _lock:
        _cache[steam_id] = {"at": time.time(), "data": result}
    return result


def get_summary(steam_id: str) -> dict:
    """Steam name, profile link and avatar. Returns {} if unavailable (not treated as an error)."""
    try:
        players = _api("ISteamUser/GetPlayerSummaries/v2/", steamids=steam_id).get("response", {}).get("players", [])
    except SteamStatsError:
        return {}
    if not players:
        return {}
    p = players[0]
    return {"name": p.get("personaname"), "url": p.get("profileurl"), "avatar": p.get("avatarfull")}


def leaderboard_entry(steam_id: str):
    """The player's Steam top-200 entry, if the leaderboard is already loaded (never fetches it)."""
    for e in steam_leaderboard._cache.get("entries", []):
        if e["profile"] == steam_id:
            return e
    return None


if __name__ == "__main__":
    sid = find_steam_id(sys.argv[1] if len(sys.argv) > 1 else input("Steam ID, link or name: "))
    s = get_user_stats(sid)
    s.pop("raw")
    print(sid, json.dumps(s, indent=1))
    print(get_summary(sid))
